#!/usr/bin/env python3
"""
Michigan Opportunity Zones Change Map Generator
================================================
Fetches ACS 5-year estimates (2017 → 2023) for Michigan OZ 1.0 census tracts
and produces a standalone michigan_oz_map.html for embedding in ArcGIS Story Maps.

Usage:
    pip install requests
    python generate_oz_map.py

Optional: add a free Census API key at api.census.gov/signup.html and set it below.
Note on geography: 2017 ACS uses 2010 census tract boundaries; 2023 ACS uses 2020
boundaries. Most Michigan tracts are identical; a small number that were split/merged
in 2020 will show "No data."
"""

# We only need four standard libraries: requests for HTTP calls, json to serialize
# the GeoJSON we embed in the HTML, time to add a short pause between paginated API
# requests, sys to exit cleanly on errors, and pathlib to write the output file.
import requests
import json
import time
import sys
from pathlib import Path


# ─── Configuration ─────────────────────────────────────────────────────────────
#
# These are the only values you need to change between runs. STATE_FIPS is the
# two-digit Federal Information Processing Standard code that the Census Bureau
# uses to identify each state — Michigan is 26. YEAR_START and YEAR_END mark the
# two survey years we compare; 2017 is when Opportunity Zone designations began,
# and 2023 is the most recent available ACS 5-year release. Adding a free Census
# API key raises the rate limit from a few requests per day to thousands.

CENSUS_API_KEY = ""
STATE_FIPS     = "26"
YEAR_START     = 2017
YEAR_END       = 2023
OUTPUT_FILE    = "michigan_oz_map.html"


# ─── ACS Variable Codes ────────────────────────────────────────────────────────
#
# The Census Bureau's American Community Survey organizes every data point under a coded variable name that references a specific cell in a specific survey table.
# Here we map six of those codes to short internal names we use throughout the script. B17001 covers poverty status, B19013 is median household income, B23025 covers employment status, and B25077 is owner-occupied home value.
# The full list of available variables is at:https://api.census.gov/data/2023/acs/acs5/variables.json

ACS_VARS = {
    "B17001_001E": "pov_denom",
    "B17001_002E": "pov_count",
    "B19013_001E": "med_income",
    "B23025_003E": "labor_force",
    "B23025_005E": "unemployed",
    "B25077_001E": "med_home_val",
}


# ─── Missing-Data Sentinels ────────────────────────────────────────────────────
#
# Rather than returning a true NULL, the Census API signals missing or suppressed
# data using large negative integers. Different codes mean different things — for
# example -999999999 means the estimate was not available, while -666666666 means
# the margin of error could not be computed — but for our purposes all of them
# mean "no usable data." We collect them in a set so we can check membership in
# constant time when cleaning each raw value.

NULL_SENTINELS = {"-666666666", "-999999999", "-333333333",
                  "-222222222", "-888888888", "null", "None", "N"}


# ─── Helper Functions ──────────────────────────────────────────────────────────
#
# These four small utilities handle all the arithmetic that appears repeatedly
# when cleaning and computing metrics. to_float converts a raw API string to a
# Python float and returns None for any missing-data value. safe_rate divides a
# numerator by a denominator only when both are present and the denominator is
# non-zero. pct_change and pp_change implement two different ways of expressing
# change between years: pct_change gives a relative percentage (used for dollar
# values like income and home prices), while pp_change gives an absolute
# percentage-point difference (used for rates like poverty and unemployment,
# where saying a rate "fell 20 percent" is misleading if it only dropped from
# 10% to 8%).

def to_float(v):
    if v is None or str(v).strip() in NULL_SENTINELS:
        return None
    try:
        f = float(v)
        return None if f < -100_000 else f
    except (ValueError, TypeError):
        return None

def safe_rate(num, denom):
    n, d = to_float(num), to_float(denom)
    return None if (n is None or d is None or d == 0) else n / d

def pct_change(old, new):
    o, n = old, new
    return None if (o is None or n is None or o == 0) else (n - o) / abs(o) * 100

def pp_change(old_rate, new_rate):
    return None if (old_rate is None or new_rate is None) else (new_rate - old_rate) * 100

def rnd(v, d=2):
    return round(v, d) if v is not None else None


# ─── Fetching OZ Tract GEOIDs ─────────────────────────────────────────────────
#
# Before we can pull any Census data we need to know which census tracts are
# officially designated as Opportunity Zones. HUD publishes this list through
# an ArcGIS feature service. We query it filtered to Michigan (STATE = '26') and
# page through the results in batches of 1,000, which is the maximum the service
# returns per request. Each tract is identified by an 11-digit GEOID built from
# the 2-digit state code, 3-digit county code, and 6-digit tract code. We
# zero-pad every GEOID to exactly 11 digits so it matches the format the Census
# API and boundary files use.

def fetch_oz_geoids():
    print("▶  Fetching Michigan OZ tract GEOIDs from HUD…")
    url = ("https://services.arcgis.com/VTyQ9soqVukalItT/arcgis/rest"
           "/services/Opportunity_Zones/FeatureServer/13/query")
    geoids, offset = [], 0
    while True:
        params = {
            "where": f"STATE = '{STATE_FIPS}'",
            "outFields": "GEOID10",
            "returnGeometry": "false",
            "resultRecordCount": 1000,
            "resultOffset": offset,
            "f": "json",
        }
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
        except Exception as e:
            sys.exit(f"\nERROR fetching OZ GEOIDs: {e}\n"
                     "Check your internet connection and try again.")
        features = r.json().get("features", [])
        if not features:
            break
        for f in features:
            attrs = f["attributes"]
            g = attrs.get("GEOID10") or attrs.get("GEOID") or attrs.get("geoid10")
            if g:
                geoids.append(str(g).zfill(11))
        if len(features) < 1000:
            break
        offset += 1000
        time.sleep(0.25)
    print(f"   → {len(geoids)} Michigan OZ tracts found")
    if not geoids:
        sys.exit("ERROR: No Michigan OZ tracts returned from HUD service.")
    return geoids


# ─── Fetching ACS Data ─────────────────────────────────────────────────────────
#
# We call the Census Bureau's ACS 5-year API once for each survey year to get
# the six indicator variables for every census tract in Michigan. We intentionally
# pull all tracts rather than only OZ tracts because it is simpler to request the
# whole state in a single API call and then filter down later. The API responds
# with a two-dimensional list where the first row is a header of column names and
# every subsequent row is one tract's data. We reshape this into a dictionary
# keyed by GEOID so we can look up any tract's data in constant time during the
# metric-computation step.

def fetch_acs(year):
    print(f"▶  Fetching {year} ACS 5-year estimates for all Michigan tracts…")
    url = f"https://api.census.gov/data/{year}/acs/acs5"
    var_codes = list(ACS_VARS.keys())
    params = {
        "get": "NAME," + ",".join(var_codes),
        "for": "tract:*",
        "in": f"state:{STATE_FIPS} county:*",
    }
    if CENSUS_API_KEY:
        params["key"] = CENSUS_API_KEY
    try:
        r = requests.get(url, params=params, timeout=120)
        r.raise_for_status()
    except Exception as e:
        sys.exit(f"\nERROR fetching {year} ACS data: {e}\n"
                 "If you're getting rate-limited, add a free API key at "
                 "api.census.gov/signup.html and set CENSUS_API_KEY above.")
    rows = r.json()
    headers = rows[0]
    result = {}
    for row in rows[1:]:
        rec = dict(zip(headers, row))
        geoid = rec["state"] + rec["county"] + rec["tract"]
        result[geoid] = {ACS_VARS[v]: to_float(rec.get(v)) for v in var_codes}
    print(f"   → {len(result)} tracts returned")
    return result


# ─── Fetching Tract Boundaries ─────────────────────────────────────────────────
#
# To draw the map we need the polygon shapes of each OZ tract. The Census Bureau
# publishes cartographic boundary files in GeoJSON format at several resolutions.
# We use the 500k-resolution file from 2019, which is detailed enough to show
# individual tracts clearly but small enough to embed in a single HTML file. Two
# mirror URLs are listed in case one is temporarily unavailable. Once downloaded,
# we filter the full statewide file down to only the OZ tracts and strip every
# property except the GEOID — the computed metrics will be added to each feature
# later in main() before the file is written out.

def fetch_geometry(oz_set):
    print("▶  Downloading Michigan census tract boundaries…")
    candidate_urls = [
        "https://www2.census.gov/geo/tiger/GENZ2019/json/cb_2019_26_tract_500k.json",
        "https://raw.githubusercontent.com/uscensusbureau/citysdk/master/v2/GeoJSON/500k/2019/26/tract.json",
    ]
    geojson = None
    for url in candidate_urls:
        try:
            print(f"   Trying {url[:70]}…")
            r = requests.get(url, timeout=180)
            r.raise_for_status()
            geojson = r.json()
            break
        except Exception as ex:
            print(f"   Failed ({ex}), trying next…")
    if not geojson:
        sys.exit("ERROR: Could not download Michigan tract boundaries. "
                 "Check your internet connection.")
    features = []
    for feat in geojson.get("features", []):
        p = feat.get("properties", {})
        geoid = (p.get("GEOID")
                 or (p.get("STATEFP", "") + p.get("COUNTYFP", "") + p.get("TRACTCE", "")))
        if str(geoid) in oz_set:
            feat["properties"] = {"GEOID": str(geoid)}
            features.append(feat)
    print(f"   → Matched {len(features)} boundary polygons")
    return {"type": "FeatureCollection", "features": features}


# ─── Computing Change Metrics ──────────────────────────────────────────────────
#
# With data for both years in hand, we loop through every OZ tract and calculate
# how each indicator changed between 2017 and 2023. Poverty and unemployment are
# treated as rates, so we first divide the count by the denominator to get a
# fraction and then take the percentage-point difference between the two years.
# Income and home value are dollar amounts, so we use a standard percent-change
# formula instead. Any tract missing data in either year is counted and skipped
# rather than silently producing a zero or a misleading result. We store both the
# change value and the raw figures for each year so the map can display them in
# hover tooltips and click popups.

def compute_metrics(oz_geoids, d17, d23):
    print("▶  Computing percent-change metrics…")
    out = {}
    missing_17, missing_23 = 0, 0
    for g in oz_geoids:
        a = d17.get(g)
        b = d23.get(g)
        if not a:
            missing_17 += 1
            continue
        if not b:
            missing_23 += 1
            continue

        pr17 = safe_rate(a["pov_count"],  a["pov_denom"])
        pr23 = safe_rate(b["pov_count"],  b["pov_denom"])
        ur17 = safe_rate(a["unemployed"], a["labor_force"])
        ur23 = safe_rate(b["unemployed"], b["labor_force"])

        out[g] = {
            "pov_change":   rnd(pp_change(pr17, pr23), 2),
            "pov_2017":     rnd(pr17 * 100 if pr17 is not None else None, 1),
            "pov_2023":     rnd(pr23 * 100 if pr23 is not None else None, 1),
            "unemp_change": rnd(pp_change(ur17, ur23), 2),
            "unemp_2017":   rnd(ur17 * 100 if ur17 is not None else None, 1),
            "unemp_2023":   rnd(ur23 * 100 if ur23 is not None else None, 1),
            "inc_change":   rnd(pct_change(a["med_income"],   b["med_income"]),   1),
            "inc_2017":     int(a["med_income"])   if a["med_income"]   is not None else None,
            "inc_2023":     int(b["med_income"])   if b["med_income"]   is not None else None,
            "hmv_change":   rnd(pct_change(a["med_home_val"], b["med_home_val"]), 1),
            "hmv_2017":     int(a["med_home_val"]) if a["med_home_val"] is not None else None,
            "hmv_2023":     int(b["med_home_val"]) if b["med_home_val"] is not None else None,
        }

    print(f"   → Metrics for {len(out)} tracts "
          f"(missing 2017: {missing_17}, missing 2023: {missing_23})")
    return out


# ─── HTML Template ─────────────────────────────────────────────────────────────
#
# Everything below is a complete, self-contained HTML file that will be written
# to disk. The only connection between this Python script and the HTML is the
# placeholder __OZ_GEOJSON__, which main() replaces with the actual GeoJSON
# string before saving. Because the data is embedded directly in the file, no
# web server is needed — the map can be opened locally in a browser or uploaded
# as a single file to any static host.

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <meta http-equiv="X-Frame-Options" content="ALLOWALL"/>
  <title>Michigan Opportunity Zones: 2017–2023 Change</title>

  <!-- Leaflet is the open-source JavaScript mapping library that renders the
       interactive map. We load it from a CDN so we don't have to bundle it. -->
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; font-family: 'Segoe UI', Arial, sans-serif; overflow: hidden; }

    /*
     * The page is a full-viewport flex column. The header has a fixed height and
     * the map div below it expands to fill whatever space remains. This way the
     * map always fills the entire browser window regardless of screen size.
     */
    #app { display: flex; flex-direction: column; height: 100vh; }

    #header {
      background: #16213e;
      color: #eee;
      padding: 8px 14px;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      z-index: 1000;
      border-bottom: 2px solid #0f3460;
      flex-shrink: 0;
    }
    #header-text h1 { font-size: 0.95rem; font-weight: 700; color: #e2b96f; }
    #header-text p  { font-size: 0.7rem; color: #999; margin-top: 1px; }

    /*
     * The metric tabs let the user switch which indicator is displayed on the map.
     * The active tab gets a filled gold background; inactive tabs are outlined.
     * They are pushed to the right side of the header with margin-left: auto.
     */
    #metric-tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-left: auto; }
    .mbtn {
      padding: 5px 13px;
      border: 2px solid #0f3460;
      border-radius: 20px;
      background: transparent;
      color: #aaa;
      font-size: 0.75rem;
      cursor: pointer;
      transition: all 0.18s;
      white-space: nowrap;
    }
    .mbtn:hover  { border-color: #e2b96f; color: #e2b96f; }
    .mbtn.active { background: #e2b96f; border-color: #e2b96f; color: #16213e; font-weight: 700; }

    #map { flex: 1; }

    /*
     * The legend and the hover info panel are positioned absolutely so they
     * float over the map. The legend sits in the bottom-right corner and shows
     * the color gradient with labeled endpoints. The info panel sits in the
     * bottom-left and reveals itself (opacity 1) when the user hovers a tract.
     * pointer-events: none on the info panel prevents it from intercepting mouse
     * events that should reach the map underneath it.
     */
    #legend {
      position: absolute;
      bottom: 30px; right: 10px;
      background: rgba(16,26,56,0.93);
      color: #eee;
      padding: 10px 14px;
      border-radius: 8px;
      z-index: 1000;
      min-width: 200px;
      font-size: 0.75rem;
      border: 1px solid #0f3460;
      box-shadow: 0 2px 8px rgba(0,0,0,0.4);
    }
    #legend h4 { font-size: 0.78rem; color: #e2b96f; margin-bottom: 7px; }
    #legend-bar { height: 13px; border-radius: 4px; margin-bottom: 4px; }
    #legend-labels { display: flex; justify-content: space-between; color: #bbb; }
    .legend-na { margin-top: 8px; display: flex; align-items: center; gap: 6px; color: #777; }
    .na-box { width: 13px; height: 13px; background: #4a4a4a; border-radius: 2px; flex-shrink: 0; }
    #legend-note { margin-top: 6px; color: #666; font-size: 0.68rem; line-height: 1.3; }

    #info {
      position: absolute;
      bottom: 30px; left: 10px;
      background: rgba(16,26,56,0.93);
      color: #eee;
      padding: 10px 14px;
      border-radius: 8px;
      z-index: 1000;
      width: 240px;
      font-size: 0.76rem;
      border: 1px solid #0f3460;
      box-shadow: 0 2px 8px rgba(0,0,0,0.4);
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.15s;
    }
    #info.show { opacity: 1; }
    #info h4 { font-size: 0.8rem; color: #e2b96f; margin-bottom: 7px; }
    .irow { display: flex; justify-content: space-between; padding: 2px 0; border-bottom: 1px solid #1a2a50; }
    .irow:last-child { border: none; }
    .ilbl { color: #999; }
    .ival { font-weight: 600; }

    /*
     * Values are colored green (pos), red (neg), or yellow (neu) depending on
     * whether the change represents an improvement or a worsening. The direction
     * of what counts as "positive" differs by metric and is handled in JavaScript.
     */
    .pos { color: #4ade80; } .neg { color: #f87171; } .neu { color: #facc15; }

    /*
     * These rules override Leaflet's default popup styles to match the dark
     * navy theme used throughout the rest of the map UI.
     */
    .leaflet-popup-content-wrapper {
      background: rgba(16,26,56,0.97) !important;
      color: #eee !important;
      border: 1px solid #0f3460 !important;
      border-radius: 8px !important;
      box-shadow: 0 4px 16px rgba(0,0,0,0.5) !important;
    }
    .leaflet-popup-tip-container { display: none; }
    .leaflet-popup-content { margin: 12px 14px !important; }
    .pu-title { font-weight: 700; color: #e2b96f; margin-bottom: 10px; font-size: 0.85rem; }
    .pu-section { margin-bottom: 9px; }
    .pu-metric-label { font-size: 0.68rem; color: #888; text-transform: uppercase; letter-spacing: 0.04em; }
    .pu-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1px 10px; font-size: 0.76rem; margin-top: 2px; }
    .pu-lbl { color: #aaa; }
    .pu-val { font-weight: 600; }
  </style>
</head>
<body>
<div id="app">
  <div id="header">
    <div id="header-text">
      <h1>Michigan Opportunity Zones &mdash; Change Over Time</h1>
      <p>ACS 5-Year Estimates &bull; 2017 &rarr; 2023 &bull; OZ 1.0 Designated Tracts &bull; Hover to explore, click for full detail</p>
    </div>
    <div id="metric-tabs">
      <button class="mbtn active" data-m="pov">Poverty Rate</button>
      <button class="mbtn" data-m="unemp">Unemployment</button>
      <button class="mbtn" data-m="inc">Median Income</button>
      <button class="mbtn" data-m="hmv">Home Value</button>
    </div>
  </div>
  <div id="map"></div>
</div>

<div id="legend">
  <h4 id="leg-title">Poverty Rate Change (pp)</h4>
  <div id="legend-bar"></div>
  <div id="legend-labels"><span id="leg-lo"></span><span>0</span><span id="leg-hi"></span></div>
  <div class="legend-na"><div class="na-box"></div> No data available</div>
  <div id="legend-note">pp = percentage-point change<br>% = percent change in dollar value</div>
</div>

<div id="info">
  <h4 id="info-title">Hover over a tract</h4>
  <div id="info-body"></div>
</div>

<script>
/*
 * OZ_DATA is the GeoJSON FeatureCollection that Python injected at build time.
 * Every feature represents one Opportunity Zone census tract. Its properties
 * object holds the GEOID and all twelve computed metric fields (change values
 * and the raw 2017/2023 figures for each of the four indicators).
 */
const OZ_DATA = __OZ_GEOJSON__;

/*
 * The M object is the single source of truth for every metric the map can
 * display. Each entry names the GeoJSON field to read, the unit label to show,
 * the min/max range the color scale spans, a formatter function for display,
 * a flag called "invert" that controls which direction of change is colored
 * green versus red, and a rows function that returns the before/after values
 * shown in the hover panel and popup. Using one config object here means that
 * the coloring, labeling, legend, and interactivity logic all draw from the
 * same place and stay consistent when one metric is changed.
 */
const M = {
  pov: {
    label: 'Poverty Rate',  unit: 'pp',
    field: 'pov_change',
    invert: true,
    range: [-20, 20],
    fmt:  v => v != null ? (v>=0?'+':'')+v.toFixed(1)+' pp' : 'N/A',
    rows: p => [
      ['2017 Rate', p.pov_2017   != null ? p.pov_2017.toFixed(1)+'%'   : 'N/A'],
      ['2023 Rate', p.pov_2023   != null ? p.pov_2023.toFixed(1)+'%'   : 'N/A'],
    ]
  },
  unemp: {
    label: 'Unemployment',  unit: 'pp',
    field: 'unemp_change',  invert: true,
    range: [-15, 10],
    fmt:  v => v != null ? (v>=0?'+':'')+v.toFixed(1)+' pp' : 'N/A',
    rows: p => [
      ['2017 Rate', p.unemp_2017 != null ? p.unemp_2017.toFixed(1)+'%' : 'N/A'],
      ['2023 Rate', p.unemp_2023 != null ? p.unemp_2023.toFixed(1)+'%' : 'N/A'],
    ]
  },
  inc: {
    label: 'Median Income', unit: '%',
    field: 'inc_change',    invert: false,
    range: [-30, 110],
    fmt:  v => v != null ? (v>=0?'+':'')+v.toFixed(1)+'%' : 'N/A',
    rows: p => [
      ['2017', p.inc_2017 != null ? '$'+p.inc_2017.toLocaleString() : 'N/A'],
      ['2023', p.inc_2023 != null ? '$'+p.inc_2023.toLocaleString() : 'N/A'],
    ]
  },
  hmv: {
    label: 'Home Value',    unit: '%',
    field: 'hmv_change',    invert: false,
    range: [-20, 200],
    fmt:  v => v != null ? (v>=0?'+':'')+v.toFixed(1)+'%' : 'N/A',
    rows: p => [
      ['2017', p.hmv_2017 != null ? '$'+p.hmv_2017.toLocaleString() : 'N/A'],
      ['2023', p.hmv_2023 != null ? '$'+p.hmv_2023.toLocaleString() : 'N/A'],
    ]
  },
};

/*
 * We use a diverging red–white–green color scale. A change of zero maps to
 * near-white; a change at the negative end of the range maps to full red (for
 * inverted metrics like poverty) or full green (for non-inverted metrics like
 * income); the positive end maps to the opposite. The lerp function linearly
 * interpolates between two RGB triplets, and getColor handles the invert flag
 * so that "better" always appears green regardless of which direction that is
 * for a given metric. Tracts with no data get a dark grey fill.
 */
const COLORS = {
  RED:   [215,  48,  39],
  WHITE: [247, 247, 247],
  GREEN: [ 26, 152,  80],
};

function lerp(a, b, t) {
  t = Math.max(0, Math.min(1, t));
  return a.map((c, i) => Math.round(c + (b[i]-c)*t));
}
function rgb(c) { return `rgb(${c[0]},${c[1]},${c[2]})`; }

function getColor(val, mKey) {
  if (val == null) return '#3a3a3a';
  const cfg = M[mKey];
  const [lo, hi] = cfg.range;
  const t = val <= 0
    ? Math.max(-1, val / Math.abs(lo))
    : Math.min(1,  val / hi);
  let c;
  if (cfg.invert) {
    c = t < 0 ? lerp(COLORS.WHITE, COLORS.GREEN, -t)
              : lerp(COLORS.WHITE, COLORS.RED,    t);
  } else {
    c = t > 0 ? lerp(COLORS.WHITE, COLORS.GREEN, t)
              : lerp(COLORS.WHITE, COLORS.RED,   -t);
  }
  return rgb(c);
}

function valueClass(val, mKey) {
  if (val == null) return '';
  const cfg = M[mKey];
  if (cfg.invert) return val < 0 ? 'pos' : val > 0 ? 'neg' : 'neu';
  else            return val > 0 ? 'pos' : val < 0 ? 'neg' : 'neu';
}

/*
 * Here we initialize the Leaflet map and add the basemap tile layer. We center
 * on Michigan and set a bounding box covering the continental United States so
 * the user cannot pan off into a blank ocean. maxBoundsViscosity of 1.0 means
 * the boundary is hard — the map snaps back rather than letting the user drag
 * slightly beyond it. The CartoDB Light basemap is a muted gray that does not
 * compete visually with the colored tract fills.
 */
const US_BOUNDS = L.latLngBounds(
  L.latLng(24.0, -127.0),
  L.latLng(50.5, -65.0)
);
const map = L.map('map', {
  center: [44.5, -85.5],
  zoom: 7,
  minZoom: 5,
  maxZoom: 16,
  maxBounds: US_BOUNDS,
  maxBoundsViscosity: 1.0,
  zoomControl: true,
});
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a> | ACS 5-yr estimates, U.S. Census Bureau',
  maxZoom: 19
}).addTo(map);

let current = 'pov';
let ozLayer = null;

/*
 * The style function tells Leaflet how to draw each polygon. It reads the
 * active metric's change value from the feature's properties and converts it
 * to a fill color using getColor. Tracts with no data are rendered at low
 * opacity so they are clearly distinguished from tracts with actual values.
 * onEach attaches the three interaction handlers — highlight on hover, restore
 * on mouseout, and show the full popup on click. renderMap tears down the
 * existing layer and rebuilds it from scratch, which is how the map redraws
 * when the user switches between metrics.
 */
function style(feat) {
  const val = feat.properties[M[current].field];
  return {
    fillColor:   getColor(val, current),
    fillOpacity: val != null ? 0.82 : 0.25,
    color:       '#0d1b3e',
    weight:      0.6,
    opacity:     0.9,
  };
}

function onEach(feat, layer) {
  layer.on({
    mouseover(e) {
      e.target.setStyle({ weight: 2.5, color: '#e2b96f', fillOpacity: 0.95 });
      e.target.bringToFront();
      showInfo(feat.properties);
    },
    mouseout(e) {
      ozLayer.resetStyle(e.target);
      document.getElementById('info').classList.remove('show');
    },
    click(e) {
      showPopup(feat.properties, e.latlng);
    },
  });
}

function renderMap() {
  if (ozLayer) map.removeLayer(ozLayer);
  ozLayer = L.geoJSON(OZ_DATA, { style, onEachFeature: onEach }).addTo(map);
  updateLegend();
}

/*
 * showInfo populates the bottom-left hover panel whenever the user moves the
 * mouse over a tract. It shows the change value for the currently active metric
 * along with the raw before-and-after figures. The panel is revealed by adding
 * the "show" class, which transitions its opacity from 0 to 1.
 */
function showInfo(p) {
  const cfg = M[current];
  const val = p[cfg.field];
  const cls = valueClass(val, current);
  const panel = document.getElementById('info');

  document.getElementById('info-title').textContent = `Tract ${p.GEOID}`;

  const extraRows = cfg.rows(p).map(([lbl, v]) =>
    `<div class="irow"><span class="ilbl">${lbl}</span><span class="ival">${v}</span></div>`
  ).join('');

  document.getElementById('info-body').innerHTML =
    `<div class="irow">
       <span class="ilbl">${cfg.label} Change</span>
       <span class="ival ${cls}">${cfg.fmt(val)}</span>
     </div>` + extraRows;

  panel.classList.add('show');
}

/*
 * showPopup is called when the user clicks a tract. Unlike the hover panel,
 * which shows only the active metric, the popup displays all four indicators
 * at once so the user can compare them for a single tract in one view. The
 * popup is rendered as a Leaflet popup anchored to the click location.
 */
function showPopup(p, latlng) {
  const sections = Object.entries(M).map(([key, cfg]) => {
    const val  = p[cfg.field];
    const cls  = valueClass(val, key);
    const rows = cfg.rows(p).map(([lbl, v]) =>
      `<span class="pu-lbl">${lbl}</span><span class="pu-val">${v}</span>`
    ).join('');
    return `
      <div class="pu-section">
        <div class="pu-metric-label">${cfg.label}</div>
        <div class="pu-grid">
          <span class="pu-lbl">Change</span>
          <span class="pu-val ${cls}">${cfg.fmt(val)}</span>
          ${rows}
        </div>
      </div>`;
  }).join('');

  L.popup({ maxWidth: 290, closeButton: true })
    .setLatLng(latlng)
    .setContent(`<div class="pu-title">Census Tract ${p.GEOID}</div>${sections}`)
    .openOn(map);
}

/*
 * updateLegend rebuilds the color-bar gradient and its endpoint labels each
 * time the active metric changes. We generate 24 evenly-spaced sample values
 * across the metric's range, convert each to a color, and assemble them into
 * a CSS linear-gradient. The label on the left shows the bottom of the range
 * and the label on the right shows the top, with zero always marked in the
 * middle since the scale is diverging around zero.
 */
function updateLegend() {
  const cfg = M[current];
  const [lo, hi] = cfg.range;
  document.getElementById('leg-title').textContent = `${cfg.label} Change (${cfg.unit})`;
  document.getElementById('leg-lo').textContent = (lo > 0 ? '+' : '') + lo + cfg.unit;
  document.getElementById('leg-hi').textContent = '+' + hi + cfg.unit;

  const stops = Array.from({ length: 24 }, (_, i) => {
    const val = lo + (hi - lo) * (i / 23);
    return getColor(val, current);
  });
  document.getElementById('legend-bar').style.background =
    `linear-gradient(to right, ${stops.join(',')})`;
}

/*
 * Each metric tab button carries a data-m attribute that matches one of the
 * keys in the M config object. Clicking a tab removes the active class from
 * all buttons, adds it to the clicked one, updates the current variable, and
 * calls renderMap so the tract colors and legend immediately reflect the new
 * metric selection.
 */
document.querySelectorAll('.mbtn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mbtn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    current = btn.dataset.m;
    renderMap();
  });
});

renderMap();
</script>
</body>
</html>
"""


# ─── Main ──────────────────────────────────────────────────────────────────────
#
# main() drives the full five-step pipeline. First we fetch the official list of
# Michigan OZ tract GEOIDs from HUD. Then we pull ACS survey data from the Census
# Bureau for both 2017 and 2023. Next we download the tract boundary polygons.
# We compute change metrics for every tract that has data in both years and merge
# those numbers into each feature's GeoJSON properties. Finally we serialize the
# enriched GeoJSON, embed it into the HTML template by replacing the placeholder
# string, and write the result to disk as a standalone HTML file.
#
# One detail worth noting: we escape any "</script>" sequences inside the JSON
# string before embedding it. Without this, the browser would interpret the
# closing tag as the end of the <script> block and break the page.

def main():
    print("\n══ Michigan OZ Change Map Generator ══\n")

    oz_geoids = fetch_oz_geoids()
    oz_set    = set(oz_geoids)

    data_2017 = fetch_acs(YEAR_START)
    data_2023 = fetch_acs(YEAR_END)

    geojson  = fetch_geometry(oz_set)
    metrics  = compute_metrics(oz_geoids, data_2017, data_2023)

    matched = 0
    for feat in geojson["features"]:
        g = feat["properties"]["GEOID"]
        m = metrics.get(g)
        if m:
            feat["properties"].update(m)
            matched += 1

    print(f"\n▶  Building HTML: {len(geojson['features'])} features, "
          f"{matched} with complete metrics")

    geojson_str = json.dumps(geojson, separators=(",", ":"))
    geojson_str = geojson_str.replace("</", "<\\/")

    html = HTML_TEMPLATE.replace("__OZ_GEOJSON__", geojson_str)
    Path(OUTPUT_FILE).write_text(html, encoding="utf-8")

    size_kb = Path(OUTPUT_FILE).stat().st_size / 1024
    print(f"\n✅  Saved → {OUTPUT_FILE}  ({size_kb:.0f} KB)\n")
    print("Next steps to embed in ArcGIS Story Maps:")
    print("  1. Host the HTML file at a public HTTPS URL")
    print("     (e.g. GitHub Pages: push to a repo, enable Pages in Settings)")
    print("  2. In Story Maps, add a 'Map' or 'Embed' block")
    print("  3. Paste the hosted URL → the map will render inside an iframe")
    print("  4. Recommended iframe size: 100% width × 600–700px height\n")


if __name__ == "__main__":
    main()
