from flask import Flask, jsonify, request, render_template_string
import sqlite3
import requests
import json
import os
import re
import math
from collections import defaultdict
from datetime import date

app = Flask(__name__)

DATA_DIR    = os.environ.get('DATA_DIR', '/data')
DB_PATH     = os.path.join(DATA_DIR, 'flyertracker.db')
CACHE_PATH  = os.path.join(DATA_DIR, 'streets_cache.json')
BBOX_FILE   = os.path.join(DATA_DIR, 'bbox.txt')
CERT_FILE   = os.path.join(DATA_DIR, 'cert.pem')
KEY_FILE    = os.path.join(DATA_DIR, 'key.pem')

DEFAULT_BBOX   = "51.27,6.06,51.42,6.23"
DEFAULT_CENTER = [51.35, 6.15]
DEFAULT_ZOOM   = 12

HEADERS      = {'User-Agent': 'HogedrukVenlo/1.0 (flyertracker)'}
CACHE_VERSION = "4"  # bump when segmentation logic changes to force cache rebuild

def _read_version():
    try:
        text = open('/config.yaml').read()
        m = re.search(r'^version:\s*["\']?([^"\'\s]+)', text, re.MULTILINE)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "unknown"

VERSION = _read_version()
MIN_SEGMENT_METERS = 20  # segments shorter than this are merged into their neighbour

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_db()
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS streets (
            osm_id     TEXT PRIMARY KEY,
            name       TEXT,
            status     TEXT DEFAULT 'none',
            house_from INTEGER,
            house_to   INTEGER,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS manual_streets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            house_from INTEGER,
            house_to   INTEGER,
            status     TEXT DEFAULT 'planned',
            date_done  DATE,
            notes      TEXT DEFAULT '',
            geometry   TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS areas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            bbox       TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    for stmt in ['ALTER TABLE streets ADD COLUMN house_from INTEGER',
                 'ALTER TABLE streets ADD COLUMN house_to INTEGER',
                 'ALTER TABLE streets ADD COLUMN parent_osm_id TEXT',
                 'ALTER TABLE streets ADD COLUMN segment_index INTEGER']:
        try:
            conn.execute(stmt)
        except Exception:
            pass

    try:
        conn.execute("UPDATE streets SET parent_osm_id = osm_id, segment_index = 0 WHERE parent_osm_id IS NULL AND segment_index IS NULL")
    except Exception:
        pass
    for k, v in [('center_lat', str(DEFAULT_CENTER[0])),
                 ('center_lng', str(DEFAULT_CENTER[1])),
                 ('zoom', str(DEFAULT_ZOOM))]:
        conn.execute('INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)', (k, v))
    conn.commit()
    conn.close()

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    conn.close()
    return row['value'] if row else default

def set_setting(key, value):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (key, str(value)))
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# OSM / Overpass
# ---------------------------------------------------------------------------

def fetch_from_overpass(bbox):
    query = f"""
[out:json][timeout:90];
(
  way["highway"]["name"]({bbox});
);
out geom;
"""
    r = requests.post('https://overpass-api.de/api/interpreter',
                      data=query, timeout=120, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def angle_between_points(p1, p2, p3):
    """Calculate angle at p2 in degrees. Returns 0-180."""
    dx1, dy1 = p1[0] - p2[0], p1[1] - p2[1]
    dx2, dy2 = p3[0] - p2[0], p3[1] - p2[1]
    dot = dx1*dx2 + dy1*dy2
    mag1 = math.sqrt(dx1*dx1 + dy1*dy1)
    mag2 = math.sqrt(dx2*dx2 + dy2*dy2)
    if mag1 == 0 or mag2 == 0:
        return 180
    cos_angle = dot / (mag1 * mag2)
    cos_angle = max(-1, min(1, cos_angle))
    return math.degrees(math.acos(cos_angle))

def polyline_length_meters(coords):
    """Flat-earth approximation of a [lon, lat] polyline length in metres."""
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        mid_lat = math.radians((lat1 + lat2) / 2)
        dlat = (lat2 - lat1) * 111_320
        dlon = (lon2 - lon1) * 111_320 * math.cos(mid_lat)
        total += math.sqrt(dlat * dlat + dlon * dlon)
    return total

def merge_short_splits(coords, indices):
    """Remove split points that would produce segments shorter than MIN_SEGMENT_METERS."""
    if len(indices) <= 2:
        return indices
    merged = [indices[0]]
    for idx in indices[1:-1]:
        if polyline_length_meters(coords[merged[-1]:idx + 1]) >= MIN_SEGMENT_METERS:
            merged.append(idx)
    merged.append(indices[-1])
    # Also merge a trailing short segment back into its predecessor
    while len(merged) > 2 and polyline_length_meters(coords[merged[-2]:merged[-1] + 1]) < MIN_SEGMENT_METERS:
        merged.pop(-2)
    return merged

def split_ways_to_segments(gj):
    """Split OSM ways at intersections, T-junctions, and 90° turns into segments."""
    ways = gj.get('features', [])
    if not ways:
        return {'type': 'FeatureCollection', 'features': []}

    # Map each coordinate to the set of way indices (integers) that use it.
    # Using integer indices avoids comparing against the OSM string ID later.
    coord_to_ways = defaultdict(set)
    for way_idx, way in enumerate(ways):
        coords = way['geometry']['coordinates']
        if len(coords) < 2:
            continue
        for coord in coords:
            coord_to_ways[tuple(coord)].add(way_idx)

    segments = []
    for way_idx, way in enumerate(ways):
        coords = way['geometry']['coordinates']
        if len(coords) < 2:
            continue

        split_indices = [0]

        for i in range(1, len(coords) - 1):
            coord_tuple = tuple(coords[i])
            # T-junction / crossing: this node is shared with at least one other way
            is_junction = len(coord_to_ways.get(coord_tuple, set())) > 1
            # Sharp bend (< 100° between incoming and outgoing direction)
            is_sharp_turn = angle_between_points(coords[i-1], coords[i], coords[i+1]) < 100

            if is_junction or is_sharp_turn:
                split_indices.append(i)

        split_indices.append(len(coords) - 1)
        split_indices = sorted(set(split_indices))
        split_indices = merge_short_splits(coords, split_indices)

        for seg_idx in range(len(split_indices) - 1):
            start = split_indices[seg_idx]
            end = split_indices[seg_idx + 1] + 1
            seg_coords = coords[start:end]

            if len(seg_coords) >= 2:
                segments.append({
                    'type': 'Feature',
                    'id': f"{way['id']}_{seg_idx}",
                    'properties': {
                        'name': way['properties'].get('name'),
                        'highway': way['properties'].get('highway'),
                        'parent_osm_id': way['id'],
                        'segment_index': seg_idx,
                    },
                    'geometry': {
                        'type': 'LineString',
                        'coordinates': seg_coords
                    }
                })

    return {'type': 'FeatureCollection', 'features': segments}

def overpass_to_geojson(data):
    features = []
    for el in data.get('elements', []):
        if el['type'] != 'way' or 'geometry' not in el:
            continue
        hw = el.get('tags', {}).get('highway', '')
        if hw in ('motorway', 'motorway_link', 'trunk', 'trunk_link'):
            continue
        coords = [[pt['lon'], pt['lat']] for pt in el['geometry']]
        features.append({
            'type': 'Feature',
            'id': str(el['id']),
            'properties': {
                'name': el.get('tags', {}).get('name', 'Onbekend'),
                'highway': hw,
            },
            'geometry': {'type': 'LineString', 'coordinates': coords}
        })
    gj = {'type': 'FeatureCollection', 'features': features}
    return split_ways_to_segments(gj)

def get_streets_geojson():
    cached_bbox = ''
    if os.path.exists(BBOX_FILE):
        try:
            cached_bbox = open(BBOX_FILE).read().strip()
        except Exception:
            pass

    # Invalidate cache when bbox or segmentation version changes
    cache_stale = cached_bbox != DEFAULT_BBOX
    if not cache_stale and os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                cached = json.load(f)
            if cached.get('cache_version') != CACHE_VERSION:
                cache_stale = True
        except Exception:
            cache_stale = True

    if cache_stale and os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)

    if not os.path.exists(CACHE_PATH):
        data = fetch_from_overpass(DEFAULT_BBOX)
        gj   = overpass_to_geojson(data)
        gj['cache_version'] = CACHE_VERSION
        with open(CACHE_PATH, 'w') as f:
            json.dump(gj, f)
        with open(BBOX_FILE, 'w') as f:
            f.write(DEFAULT_BBOX)
        return gj

    with open(CACHE_PATH) as f:
        return json.load(f)

def load_cache():
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH) as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Flyer Tracker – Hogedruk Venlo</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html{background:#1565c0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;display:flex;flex-direction:column;height:100vh;height:100dvh;background:#f0f2f5;overflow:hidden}

/* Header */
#hdr{background:#1565c0;color:#fff;padding:12px 16px;padding-top:calc(12px + env(safe-area-inset-top));display:flex;align-items:center;justify-content:space-between;flex-shrink:0;z-index:100}
#hdr-title{font-size:16px;font-weight:700;letter-spacing:-.3px}
#hdr-sub{font-size:10px;color:rgba(255,255,255,.5);margin-top:1px}
#hdr-right{display:flex;align-items:center;gap:8px}
#hdr-ver{font-size:10px;color:rgba(255,255,255,.38)}
#hdr-reset{background:rgba(255,255,255,.13);border:none;color:rgba(255,255,255,.75);width:32px;height:32px;border-radius:9px;cursor:pointer;display:flex;align-items:center;justify-content:center}
#hdr-reset:active{background:rgba(255,255,255,.22)}

/* Content */
#content{flex:1;overflow:hidden;position:relative}
.tab{display:none;height:100%;flex-direction:column}
.tab.on{display:flex}

/* Bottom nav */
#nav{display:flex;background:#fff;border-top:1px solid #e8eaed;flex-shrink:0;padding-bottom:env(safe-area-inset-bottom)}
.nb{flex:1;padding:9px 4px 9px;border:none;background:none;cursor:pointer;font-size:10px;color:#94a3b8;display:flex;flex-direction:column;align-items:center;gap:4px;font-weight:500;transition:color .15s}
.nb svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
.nb.on{color:#1565c0}

/* Map */
#map{flex:1}

/* GPS button */
#gpsbtn{position:fixed;bottom:calc(76px + env(safe-area-inset-bottom));left:12px;width:44px;height:44px;background:#fff;border:none;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 14px rgba(0,0,0,.18);z-index:1000;color:#64748b;transition:bottom .2s ease,color .2s,box-shadow .2s}
#gpsbtn.on{color:#1565c0;box-shadow:0 2px 14px rgba(21,101,192,.35)}

/* Legend */
#legend{position:fixed;bottom:calc(70px + env(safe-area-inset-bottom));right:10px;background:#fff;border-radius:12px;padding:8px 13px;box-shadow:0 2px 14px rgba(0,0,0,.1);z-index:1000;font-size:11px;line-height:2;color:#374151}
#legend div{display:flex;align-items:center;gap:8px}
.ll{width:18px;height:3px;border-radius:2px;flex-shrink:0}

/* Street popup */
#popup{position:fixed;bottom:0;left:0;right:0;background:#fff;border-radius:20px 20px 0 0;padding:18px 18px calc(32px + env(safe-area-inset-bottom));box-shadow:0 -4px 24px rgba(0,0,0,.12);z-index:2000;display:none}
#popup-name{font-size:17px;font-weight:700;margin-bottom:3px;color:#111}
#popup-cur{font-size:12px;color:#94a3b8;margin-bottom:16px}
#xpop{position:absolute;top:14px;right:16px;font-size:20px;color:#d1d5db;cursor:pointer;line-height:1}
.brow{display:flex;gap:8px}
.btn{flex:1;padding:13px 6px;border:none;border-radius:12px;font-size:13px;font-weight:700;cursor:pointer;letter-spacing:.1px}
.btn:active{filter:brightness(.9)}
.bpl{background:#f59e0b;color:#fff}
.bdo{background:#16a34a;color:#fff}
.bno{background:#f1f5f9;color:#64748b}

/* Loading */
#loading{position:fixed;inset:0;background:rgba(255,255,255,.95);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:5000}
#loading p{margin-top:15px;font-size:14px;color:#374151;font-weight:500}
#loading small{margin-top:5px;font-size:12px;color:#94a3b8}
.spin{width:36px;height:36px;border:3px solid #e2e8f0;border-top-color:#1565c0;border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* Tab 2 – Straten */
#streets-scroll{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.card{background:#fff;border-radius:14px;padding:14px;box-shadow:0 1px 6px rgba(0,0,0,.06)}
.card h3{font-size:13px;font-weight:700;color:#1e293b;margin-bottom:11px;letter-spacing:-.1px}
.frow{display:flex;gap:8px;margin-bottom:9px}
.fcol{display:flex;flex-direction:column;gap:3px;flex:1}
.fcol label{font-size:10px;color:#94a3b8;font-weight:700;letter-spacing:.5px;text-transform:uppercase}
.fcol input,.fcol select{padding:9px 10px;border:1.5px solid #e2e8f0;border-radius:9px;font-size:14px;outline:none;width:100%;background:#fff;color:#111}
.fcol input:focus,.fcol select:focus{border-color:#1565c0}
.si{border:1.5px solid #f1f5f9;border-radius:12px;padding:11px;margin-bottom:7px;display:flex;align-items:flex-start;gap:8px}
.si:last-child{margin-bottom:0}
.sinfo{flex:1}
.sname{font-weight:700;font-size:14px;color:#1e293b}
.smeta{font-size:11px;color:#94a3b8;margin-top:2px}
.sbadge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;flex-shrink:0;white-space:nowrap}
.bpl2{background:#fef3c7;color:#92400e}
.bdo2{background:#dcfce7;color:#166534}
.delbtn{background:none;border:none;color:#cbd5e1;font-size:18px;cursor:pointer;padding:0 3px;flex-shrink:0}
.delbtn:hover{color:#ef4444}
.empty{color:#94a3b8;font-size:13px;text-align:center;padding:22px}
.vmsg{font-size:11px;margin-top:3px}

/* Tab 3 – Stats */
#stats-scroll{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}
.stcard{background:#fff;border-radius:16px;padding:18px;box-shadow:0 2px 10px rgba(0,0,0,.05)}
.st-hero{text-align:center;padding:28px 18px 22px}
.st-hero-num{font-size:56px;font-weight:800;letter-spacing:-2px;line-height:1;color:#111}
.st-hero-lbl{font-size:13px;color:#6b7280;margin-top:9px;font-weight:500}
.st-hero-sub{font-size:11px;color:#9ca3af;margin-top:4px}
.stgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stgrid .stcard{padding:14px 16px}
.sttitle{font-size:10px;color:#9ca3af;font-weight:700;letter-spacing:.6px;text-transform:uppercase;margin-bottom:6px}
.stval{font-size:28px;font-weight:800;letter-spacing:-.5px;line-height:1.1}
.stsub{font-size:11px;color:#9ca3af;margin-top:3px}
.pbar-hdr{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.pbar-lbl{font-size:13px;color:#374151;font-weight:600}
.pbar-pct{font-size:20px;font-weight:800;color:#16a34a}
.pbar{height:8px;background:#f1f5f9;border-radius:4px;overflow:hidden}
.pfill{height:100%;background:linear-gradient(90deg,#16a34a,#22c55e);border-radius:4px;transition:width .6s ease}
.pbar-sub{font-size:11px;color:#9ca3af;margin-top:7px}

/* GPS street bar */
#gps-bar{position:fixed;bottom:calc(56px + env(safe-area-inset-bottom));left:0;right:0;background:#1565c0;color:#fff;padding:10px 14px;display:none;align-items:center;gap:8px;z-index:1500;font-size:13px;box-shadow:0 -2px 12px rgba(21,101,192,.3)}
#gps-bar .gs{flex:1;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gbtn{padding:6px 13px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;flex-shrink:0}

/* Bulk modal */
#bulk-modal{position:fixed;bottom:0;left:0;right:0;background:#fff;border-radius:20px 20px 0 0;padding:18px 18px calc(32px + env(safe-area-inset-bottom));box-shadow:0 -4px 24px rgba(0,0,0,.12);z-index:2000;display:none;max-height:80vh;overflow-y:auto}
#bulk-modal h3{font-size:15px;font-weight:700;color:#1e293b;margin-bottom:12px}
#bulk-modal textarea{width:100%;height:120px;padding:10px;border:1.5px solid #e2e8f0;border-radius:10px;font-size:13px;font-family:monospace;resize:vertical;outline:none}
#bulk-modal textarea:focus{border-color:#1565c0}
#bulk-modal-results{margin-top:14px;display:none}
.bulk-summary{font-size:13px;font-weight:600;margin-bottom:10px}
.bulk-notfound{font-size:12px;color:#6b7280;margin-top:8px;max-height:120px;overflow-y:auto;padding:8px;background:#f8fafc;border-radius:8px}
.bulk-notfound div{padding:2px 0}
#xbulk{position:absolute;top:14px;right:16px;font-size:20px;color:#d1d5db;cursor:pointer;line-height:1}
</style>
</head>
<body>

<div id="hdr">
  <div><div id="hdr-title">Flyer Tracker</div><div id="hdr-sub">Hogedruk Venlo</div></div>
  <div id="hdr-right">
    <span id="hdr-ver">v{{ version }}</span>
    <button id="hdr-reset" onclick="resetStreets()" title="Reset alle straten"><svg viewBox="0 0 24 24" width="15" height="15" stroke="currentColor" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a1 1 0 011-1h4a1 1 0 011 1v2"/></svg></button>
  </div>
</div>

<div id="content">

  <!-- TAB 1: KAART -->
  <div class="tab on" id="tab-map">
    <div id="map"></div>
  </div>

  <!-- TAB 2: STRATEN -->
  <div class="tab" id="tab-streets">
    <div id="streets-scroll">
      <div class="card">
        <h3>Straat toevoegen</h3>
        <div class="frow">
          <div class="fcol" style="flex:2">
            <label>STRAATNAAM</label>
            <input id="s-name" type="text" placeholder="Kerkstraat" list="street-names-list" autocomplete="off" oninput="onStreetInput()">
            <datalist id="street-names-list"></datalist>
            <div id="s-name-msg" class="vmsg" style="display:none"></div>
          </div>
          <div class="fcol">
            <label>STATUS</label>
            <select id="s-status">
              <option value="planned">Plannen</option>
              <option value="done">Gedaan</option>
            </select>
          </div>
        </div>
        <button class="btnp" style="width:100%;padding:11px;background:#1565c0;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer" onclick="addStreet()">Toevoegen</button>
      </div>

      <div class="card">
        <h3>Bulk toevoegen</h3>
        <p style="font-size:12px;color:#999;margin-bottom:10px">Plak meerdere straatnamen (per regel of komma-gescheiden)</p>
        <button class="btnp" style="width:100%;padding:11px;background:#8b5cf6;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer" onclick="openBulkModal()">Modal openen</button>
      </div>

      <div class="card">
        <h3>Overzicht</h3>
        <div id="street-list"><p class="empty">Nog geen straten toegevoegd</p></div>
      </div>
    </div>
  </div>

  <!-- TAB 3: STATISTIEKEN -->
  <div class="tab" id="tab-stats">
    <div id="stats-scroll">
      <div class="stcard st-hero">
        <div class="st-hero-num" id="st-houses">–</div>
        <div class="st-hero-lbl">geschatte woningen bereikt</div>
        <div class="st-hero-sub" id="st-km-lbl">–</div>
      </div>
      <div class="stcard">
        <div class="pbar-hdr">
          <span class="pbar-lbl">Eigen voortgang</span>
          <span class="pbar-pct" id="st-pct">–%</span>
        </div>
        <div class="pbar"><div class="pfill" id="st-bar" style="width:0%"></div></div>
        <div class="pbar-sub" id="st-plan-sub">–</div>
      </div>
      <div class="stgrid">
        <div class="stcard">
          <div class="sttitle">Gedaan</div>
          <div class="stval" id="st-done" style="color:#16a34a">–</div>
          <div class="stsub">straten</div>
        </div>
        <div class="stcard">
          <div class="sttitle">Gepland</div>
          <div class="stval" id="st-pl" style="color:#d97706">–</div>
          <div class="stsub">nog te doen</div>
        </div>
        <div class="stcard">
          <div class="sttitle">Geflyerd</div>
          <div class="stval" id="st-km" style="color:#1565c0">–</div>
          <div class="stsub">kilometer</div>
        </div>
        <div class="stcard">
          <div class="sttitle">Segmenten</div>
          <div class="stval" id="st-segs" style="color:#7c3aed">–</div>
          <div class="stsub">gedaan</div>
        </div>
      </div>
    </div>
  </div>

</div>

<!-- Bottom nav -->
<div id="nav">
  <button class="nb on" id="nb-map" onclick="goTab('map')"><svg viewBox="0 0 24 24"><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6"/><line x1="8" y1="2" x2="8" y2="18"/><line x1="16" y1="6" x2="16" y2="22"/></svg>Kaart</button>
  <button class="nb" id="nb-streets" onclick="goTab('streets')"><svg viewBox="0 0 24 24"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>Straten</button>
  <button class="nb" id="nb-stats" onclick="goTab('stats')"><svg viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="2" y1="20" x2="22" y2="20"/></svg>Statistieken</button>
</div>

<!-- GPS btn -->
<button id="gpsbtn" onclick="toggleGPS()"><svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/></svg></button>

<!-- Legend -->
<div id="legend">
  <div><span class="ll" style="background:#aaa"></span>Niet gedaan</div>
  <div><span class="ll" style="background:#f59e0b"></span>Gepland</div>
  <div><span class="ll" style="background:#16a34a"></span>Gedaan</div>
</div>

<!-- Popup -->
<div id="popup">
  <span id="xpop" onclick="closePop()">✕</span>
  <div id="popup-name"></div>
  <div id="popup-cur"></div>
  <div class="brow" style="margin-top:14px">
    <button class="btn bpl" onclick="setStatus('planned')">Plannen</button>
    <button class="btn bdo" onclick="setStatus('done')">Gedaan</button>
    <button class="btn bno" onclick="setStatus('none')">Reset</button>
  </div>
</div>

<!-- GPS bar -->
<div id="gps-bar">
  <span class="gs" id="gps-street">–</span>
  <button class="gbtn" style="background:rgba(255,255,255,.2);color:#fff" onclick="markDetectedStreet('planned')">Plannen</button>
  <button class="gbtn" style="background:#fff;color:#1565c0" onclick="markDetectedStreet('done')">Gedaan</button>
</div>

<!-- Bulk modal -->
<div id="bulk-modal">
  <span id="xbulk" onclick="closeBulkModal()">✕</span>
  <h3>Bulk straten toevoegen</h3>
  <textarea id="bulk-input" placeholder="Kerkstraat&#10;Ringlaan&#10;Marktplein"></textarea>
  <div class="brow" style="margin-top:14px">
    <button class="btn bpl" onclick="processBulkAdd()">✅ Toevoegen</button>
    <button class="btn bno" onclick="closeBulkModal()">Annuleren</button>
  </div>
  <div id="bulk-modal-results">
    <div class="bulk-summary" id="bulk-summary"></div>
    <button id="bulk-toggle-notfound" style="font-size:11px;color:#1565c0;background:none;border:none;cursor:pointer;text-decoration:underline;display:none" onclick="toggleBulkNotFound()">Toon niet gevonden</button>
    <div class="bulk-notfound" id="bulk-notfound" style="display:none"></div>
  </div>
</div>

<!-- Loading -->
<div id="loading">
  <div class="spin"></div>
  <p id="load-msg">Straten ophalen…</p>
  <small id="load-sub">Eerste keer duurt ~30 seconden</small>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
// ============================================================
// TABS
// ============================================================
function goTab(t) {
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('on'));
  document.querySelectorAll('.nb').forEach(el => el.classList.remove('on'));
  document.getElementById('tab-' + t).classList.add('on');
  document.getElementById('nb-' + t).classList.add('on');
  const isMap = t === 'map';
  document.getElementById('gpsbtn').style.display = isMap ? 'flex' : 'none';
  document.getElementById('legend').style.display  = isMap ? 'block' : 'none';
  if (isMap) setTimeout(() => map.invalidateSize(), 60);
  if (t === 'streets') { loadStreetList(); loadStreetNames(); }
  if (t === 'stats')   loadStats();
}

// ============================================================
// MAP
// ============================================================
const COL = { none:'#aaaaaa', planned:'#f59e0b', done:'#16a34a' };
const WGT = { none:2, planned:4, done:4 };
const OPC = { none:.4, planned:1, done:1 };
const LBL = { none:'Niet gedaan', planned:'Gepland', done:'Gedaan' };

const map = L.map('map').setView([51.35, 6.15], 12);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution:'© OpenStreetMap', maxZoom:19
}).addTo(map);

let sLayers      = {};  // osm_id → {vis, hit}
let statuses     = {};  // osm_id → status string
let segsByName   = {};  // street name → [osm_id, ...]
let allFeatures  = [];  // all GeoJSON features for GPS detection
let selId        = null;
let selName      = null;
let locMark      = null;
let watchId      = null;
let detectedId   = null;
let detectedName = null;

async function loadStatuses() {
  const r   = await fetch('/api/status');
  const raw = await r.json();
  statuses  = {};
  for (const [id, val] of Object.entries(raw)) {
    statuses[id] = val.status;
  }
}

function applyStyle(id) {
  const s = statuses[id] || 'none';
  if (sLayers[id]) sLayers[id].vis.setStyle({ color:COL[s], weight:WGT[s], opacity:OPC[s] });
}

// --- popup ---
function openPop(id, name) {
  selId   = id;
  selName = name;
  document.getElementById('popup-name').textContent = name;
  const segs  = segsByName[name] || [];
  const done  = segs.filter(sid => statuses[sid] === 'done').length;
  const plan  = segs.filter(sid => statuses[sid] === 'planned').length;
  const total = segs.length;
  const pct   = total > 0 ? Math.round((done + plan) / total * 100) : 0;
  document.getElementById('popup-cur').textContent =
    LBL[statuses[id] || 'none'] + ' · ' + (done + plan) + '/' + total + ' segm. (' + pct + '%)';
  document.getElementById('popup').style.display = 'block';
}

function closePop() {
  document.getElementById('popup').style.display = 'none';
  selId = null; selName = null;
}
map.on('click', closePop);

async function setStatus(s) {
  if (!selId) return;
  const id   = selId;
  const name = selName;
  closePop();
  await fetch('/api/status', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id, name, status:s, house_from:null, house_to:null})
  });
  statuses[id] = s;
  applyStyle(id);
}

// --- load streets ---
async function loadStreets() {
  showLoad('Straten ophalen…', 'Eerste keer duurt ~30 seconden');
  try {
    await loadStatuses();
    const r   = await fetch('/api/streets');
    const gj  = await r.json();
    if (gj.error) throw new Error(gj.error);

    Object.values(sLayers).forEach(l => { map.removeLayer(l.vis); map.removeLayer(l.hit); });
    sLayers     = {};
    segsByName  = {};
    allFeatures = gj.features;

    gj.features.forEach(f => {
      const id   = f.id;
      const name = f.properties.name || 'Onbekend';
      const s    = statuses[id] || 'none';

      if (!segsByName[name]) segsByName[name] = [];
      segsByName[name].push(id);

      const vis = L.geoJSON(f, { style:{ color:COL[s], weight:WGT[s], opacity:OPC[s] } }).addTo(map);
      const hit = L.geoJSON(f, { style:{ color:'transparent', weight:22, opacity:0 } }).addTo(map);
      hit.on('click', e => { L.DomEvent.stopPropagation(e); openPop(id, name); });

      sLayers[id] = { vis, hit };
    });

    hideLoad();
  } catch (err) {
    document.getElementById('loading').innerHTML =
      '<p style="color:red;padding:20px">Fout: ' + err.message + '</p>';
  }
}

// --- GPS ---
function toggleGPS() {
  const btn = document.getElementById('gpsbtn');
  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
    if (locMark) { map.removeLayer(locMark); locMark = null; }
    setGpsBar(false);
    detectedId = null; detectedName = null;
    btn.classList.remove('on');
  } else {
    btn.classList.add('on');
    if (!navigator.geolocation) {
      alert('Locatie wordt niet ondersteund door deze browser.');
      btn.classList.remove('on');
      return;
    }
    watchId = navigator.geolocation.watchPosition(pos => {
      const { latitude:lat, longitude:lng } = pos.coords;
      if (!locMark) {
        locMark = L.circleMarker([lat,lng], {
          radius:10, fillColor:'#1565c0', color:'#fff', weight:3, fillOpacity:1
        }).addTo(map);
        map.setView([lat,lng], 17);
      } else {
        locMark.setLatLng([lat,lng]);
      }
      updateGpsBar(lat, lng);
    }, err => {
      const msg = err.code === 1
        ? 'Locatietoegang geweigerd. Gebruik HTTPS of open de app op hetzelfde apparaat via localhost.'
        : err.code === 2 ? 'Locatie niet beschikbaar.' : 'Locatie time-out.';
      alert(msg);
      btn.classList.remove('on');
      watchId = null;
    }, { enableHighAccuracy:true });
  }
}

function pointToSegDist(lat, lng, lat1, lng1, lat2, lng2) {
  const R = 6371000, rad = Math.PI / 180;
  const scaleX = Math.cos((lat1 + lat2) / 2 * rad);
  const dy = (lat2 - lat1) * R * rad, dx = (lng2 - lng1) * R * rad * scaleX;
  const py = (lat  - lat1) * R * rad, px = (lng  - lng1) * R * rad * scaleX;
  const len2 = dx*dx + dy*dy;
  const t = len2 > 0 ? Math.max(0, Math.min(1, (px*dx + py*dy) / len2)) : 0;
  return Math.sqrt((px - t*dx)**2 + (py - t*dy)**2);
}

function updateGpsBar(lat, lng) {
  let bestId = null, bestName = null, bestDist = 30;
  for (const f of allFeatures) {
    const c = f.geometry.coordinates;
    for (let i = 0; i < c.length - 1; i++) {
      const d = pointToSegDist(lat, lng, c[i][1], c[i][0], c[i+1][1], c[i+1][0]);
      if (d < bestDist) { bestDist = d; bestId = f.id; bestName = f.properties.name; }
    }
  }
  if (bestId) {
    detectedId   = bestId;
    detectedName = bestName;
    document.getElementById('gps-street').textContent = bestName;
    setGpsBar(true);
  } else {
    detectedId = null; detectedName = null;
    setGpsBar(false);
  }
}

function setGpsBar(visible) {
  document.getElementById('gps-bar').style.display = visible ? 'flex' : 'none';
  const sab = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sab') || '0') || 0;
  document.getElementById('gpsbtn').style.bottom = visible
    ? 'calc(110px + env(safe-area-inset-bottom))'
    : 'calc(76px + env(safe-area-inset-bottom))';
}

async function markDetectedStreet(status) {
  if (!detectedId) return;
  await fetch('/api/status', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id: detectedId, name: detectedName, status, house_from: null, house_to: null })
  });
  statuses[detectedId] = status;
  applyStyle(detectedId);
}

// --- Bulk add ---
function openBulkModal() {
  document.getElementById('bulk-modal').style.display = 'block';
  document.getElementById('bulk-input').value = '';
  document.getElementById('bulk-modal-results').style.display = 'none';
  document.getElementById('bulk-notfound').style.display = 'none';
  document.getElementById('bulk-toggle-notfound').style.display = 'none';
}

function closeBulkModal() {
  document.getElementById('bulk-modal').style.display = 'none';
}

async function processBulkAdd() {
  const input = document.getElementById('bulk-input').value.trim();
  if (!input) { alert('Vul straten in'); return; }

  const lines = input.split(/[\n,;]/).map(s => s.trim()).filter(s => s && s.length > 0);
  if (lines.length === 0) { alert('Geen straten gevonden'); return; }

  const unique = [...new Set(lines)];
  const valid = [];
  const notfound = [];

  for (const line of unique) {
    const canonical = streetNames.find(n => n.toLowerCase() === line.toLowerCase());
    if (canonical) valid.push(canonical);
    else notfound.push(line);
  }

  if (valid.length === 0) {
    document.getElementById('bulk-summary').textContent = '❌ Geen straten gevonden';
    document.getElementById('bulk-modal-results').style.display = 'block';
    return;
  }

  let added = 0;
  for (const name of valid) {
    try {
      const r = await fetch('/api/mark-by-name', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ name, status: 'planned' })
      });
      const result = await r.json();
      if (r.ok) {
        added += 1;
        (result.osm_ids || []).forEach(id => { statuses[id] = 'planned'; applyStyle(id); });
      }
    } catch (e) {}
  }

  const summary = `✅ ${added} toegevoegd` + (notfound.length > 0 ? ` · ❌ ${notfound.length} niet gevonden` : '');
  document.getElementById('bulk-summary').textContent = summary;
  if (notfound.length > 0) {
    document.getElementById('bulk-notfound').innerHTML = notfound.map(n => '<div>• ' + n + '</div>').join('');
    document.getElementById('bulk-toggle-notfound').style.display = 'inline';
  }
  document.getElementById('bulk-modal-results').style.display = 'block';
  loadStreetList();
}

function toggleBulkNotFound() {
  const el = document.getElementById('bulk-notfound');
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

function showLoad(msg, sub) {
  document.getElementById('load-msg').textContent = msg;
  document.getElementById('load-sub').textContent = sub;
  document.getElementById('loading').style.display = 'flex';
}
function hideLoad() { document.getElementById('loading').style.display = 'none'; }

// ============================================================
// STREETS TAB (TAB 2)
// ============================================================
let streetNames  = [];
let currentMarked = [];

async function loadStreetNames() {
  if (streetNames.length) return;  // already loaded
  const r = await fetch('/api/street-names');
  streetNames = await r.json();
  const dl = document.getElementById('street-names-list');
  dl.innerHTML = streetNames.map(n => '<option value="' + n + '">').join('');
}

function onStreetInput() {
  const name = document.getElementById('s-name').value.trim();
  const msg  = document.getElementById('s-name-msg');
  if (!name || streetNames.length === 0) { msg.style.display = 'none'; return; }
  const found = streetNames.some(n => n.toLowerCase() === name.toLowerCase());
  msg.textContent   = found ? '✓ Straat gevonden' : '✗ Straat niet gevonden in het kaartgebied';
  msg.style.color   = found ? '#16a34a' : '#ef4444';
  msg.style.display = 'block';
}

async function loadStreetList() {
  const r = await fetch('/api/streets-marked');
  currentMarked = await r.json();
  const list = document.getElementById('street-list');

  if (!currentMarked.length) {
    list.innerHTML = '<p class="empty">Nog geen straten toegevoegd</p>';
    return;
  }

  list.innerHTML = currentMarked.map(function(s, i) {
    const marked = s.segments_done + s.segments_planned;
    const badge  = s.status === 'done' ? 'bdo2' : 'bpl2';
    const label  = s.status === 'done' ? '✅ Gedaan' : '📌 Gepland';
    return '<div class="si">' +
      '<div class="sinfo">' +
        '<div class="sname">' + s.name + '</div>' +
        '<div class="smeta">' + marked + '/' + s.segments_total + ' segm. · ' + s.coverage_pct + '% gedekt</div>' +
      '</div>' +
      '<span class="sbadge ' + badge + '">' + label + '</span>' +
      '<button class="delbtn" onclick="resetByName(' + i + ')">×</button>' +
    '</div>';
  }).join('');
}

async function addStreet() {
  const nameInput = document.getElementById('s-name');
  const inputName = nameInput.value.trim();
  const status    = document.getElementById('s-status').value;
  const msg       = document.getElementById('s-name-msg');

  if (!inputName) { alert('Vul een straatnaam in'); return; }

  const canonical = streetNames.find(n => n.toLowerCase() === inputName.toLowerCase());
  if (!canonical) {
    msg.textContent   = '✗ Straat niet gevonden. Kies een straat uit het kaartgebied.';
    msg.style.color   = '#ef4444';
    msg.style.display = 'block';
    return;
  }

  const r = await fetch('/api/mark-by-name', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name: canonical, status })
  });
  const result = await r.json();

  if (!r.ok) {
    msg.textContent   = result.error || 'Fout bij opslaan';
    msg.style.color   = '#ef4444';
    msg.style.display = 'block';
    return;
  }

  (result.osm_ids || []).forEach(function(id) {
    statuses[id] = status;
    applyStyle(id);
  });

  nameInput.value   = '';
  msg.style.display = 'none';
  loadStreetList();
}

async function resetByName(idx) {
  const s = currentMarked[idx];
  if (!s) return;
  if (!confirm('Alle segmenten van "' + s.name + '" resetten?')) return;

  const r = await fetch('/api/mark-by-name', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name: s.name, status: 'none' })
  });
  const result = await r.json();

  (result.osm_ids || []).forEach(function(id) {
    statuses[id] = 'none';
    applyStyle(id);
  });
  loadStreetList();
}

// ============================================================
// STATS (TAB 3)
// ============================================================
function computeStats() {
  let doneM = 0, plannedM = 0, doneSegs = 0, plannedSegs = 0;
  const doneNames = new Set(), plannedNames = new Set();
  for (const f of allFeatures) {
    const st = statuses[f.id] || 'none';
    if (st === 'none') continue;
    const c = f.geometry.coordinates;
    let m = 0;
    for (let i = 0; i < c.length - 1; i++) {
      const dlat = (c[i+1][1] - c[i][1]) * 111320;
      const dlon = (c[i+1][0] - c[i][0]) * 111320 * Math.cos((c[i][1] + c[i+1][1]) / 2 * Math.PI / 180);
      m += Math.sqrt(dlat * dlat + dlon * dlon);
    }
    if (st === 'done')    { doneM += m;    doneSegs++;    if (f.properties.name) doneNames.add(f.properties.name); }
    if (st === 'planned') { plannedM += m; plannedSegs++; if (f.properties.name) plannedNames.add(f.properties.name); }
  }
  return { doneM, plannedM, doneSegs, plannedSegs, doneStreets: doneNames.size, plannedStreets: plannedNames.size };
}

async function loadStats() {
  const s     = computeStats();
  const total = s.doneSegs + s.plannedSegs;
  const pct   = total > 0 ? Math.round(s.doneSegs / total * 100) : 0;
  const houses = Math.round(s.doneM * 0.05 / 5) * 5;
  const km     = (s.doneM / 1000).toFixed(1);

  document.getElementById('st-houses').textContent   = houses > 0 ? '~' + houses : '–';
  document.getElementById('st-km-lbl').textContent   = s.doneM > 0 ? km + ' km geflyerd' : 'Nog niks gedaan';
  document.getElementById('st-pct').textContent      = pct + '%';
  document.getElementById('st-bar').style.width      = pct + '%';
  document.getElementById('st-plan-sub').textContent = s.doneSegs + ' van ' + total + ' geplande segmenten gedaan';
  document.getElementById('st-done').textContent     = s.doneStreets;
  document.getElementById('st-pl').textContent       = s.plannedStreets;
  document.getElementById('st-km').textContent       = km;
  document.getElementById('st-segs').textContent     = s.doneSegs;
}

async function resetStreets() {
  if (!confirm('Alle straat-statussen verwijderen en opnieuw beginnen?')) return;
  await fetch('/api/reset-streets', { method: 'POST' });
  statuses = {};
  Object.keys(sLayers).forEach(id => applyStyle(id));
}

// ============================================================
// INIT
// ============================================================

// Fix iOS standalone webapp viewport height bug (landscape→portrait glitch)
function fixLayout() {
  const h = window.innerHeight;
  document.getElementById('content').style.height =
    (h - document.getElementById('hdr').offsetHeight - document.getElementById('nav').offsetHeight) + 'px';
  map.invalidateSize();
}
window.addEventListener('resize', fixLayout);
window.addEventListener('orientationchange', () => setTimeout(fixLayout, 150));
window.addEventListener('load', fixLayout);

loadStreets();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template_string(HTML, version=VERSION)

# --- OSM streets ---
@app.route('/api/streets')
def api_streets():
    try:
        return jsonify(get_streets_geojson())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- Street names for autocomplete ---
@app.route('/api/street-names')
def api_street_names():
    gj = load_cache()
    if not gj:
        return jsonify([])
    names = sorted({f['properties']['name'] for f in gj.get('features', [])
                    if f['properties'].get('name')})
    return jsonify(names)

# --- Mark all segments of a street by name ---
@app.route('/api/mark-by-name', methods=['POST'])
def api_mark_by_name():
    d      = request.get_json()
    name   = d.get('name', '').strip()
    status = d.get('status', 'planned')

    gj = load_cache()
    if not gj:
        return jsonify({'error': 'Kaartgegevens niet geladen'}), 400

    matching = [f for f in gj.get('features', [])
                if f['properties'].get('name', '').lower() == name.lower()]
    if not matching:
        return jsonify({'error': 'Straat niet gevonden in het gebied'}), 404

    conn = get_db()
    for feat in matching:
        if status == 'none':
            conn.execute(
                "UPDATE streets SET status='none', house_from=NULL, house_to=NULL, "
                "updated_at=CURRENT_TIMESTAMP WHERE osm_id=?",
                (feat['id'],)
            )
        else:
            conn.execute('''
                INSERT INTO streets (osm_id, name, status) VALUES (?,?,?)
                ON CONFLICT(osm_id) DO UPDATE SET
                    name=excluded.name, status=excluded.status,
                    house_from=NULL, house_to=NULL,
                    updated_at=CURRENT_TIMESTAMP
            ''', (feat['id'], feat['properties']['name'], status))
    conn.commit()
    conn.close()

    return jsonify({
        'ok':      True,
        'count':   len(matching),
        'name':    matching[0]['properties']['name'],
        'osm_ids': [f['id'] for f in matching]
    })

# --- Street statuses ---
@app.route('/api/status', methods=['GET'])
def api_status_get():
    conn = get_db()
    rows = conn.execute('SELECT osm_id, name, status, house_from, house_to FROM streets').fetchall()
    conn.close()
    return jsonify({r['osm_id']: {
        'status': r['status'], 'name': r['name'],
        'house_from': r['house_from'], 'house_to': r['house_to']
    } for r in rows})

@app.route('/api/status', methods=['POST'])
def api_status_post():
    d = request.get_json()
    conn = get_db()
    conn.execute('''
        INSERT INTO streets (osm_id, name, status, house_from, house_to) VALUES (?,?,?,?,?)
        ON CONFLICT(osm_id) DO UPDATE SET
            name=COALESCE(excluded.name, streets.name),
            status=excluded.status,
            house_from=excluded.house_from,
            house_to=excluded.house_to,
            updated_at=CURRENT_TIMESTAMP
    ''', (d['id'], d.get('name'), d['status'], d.get('house_from'), d.get('house_to')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# --- Streets marked — grouped by name with segment coverage ---
@app.route('/api/streets-marked')
def api_streets_marked():
    conn = get_db()
    rows = conn.execute(
        "SELECT osm_id, name, status FROM streets WHERE status != 'none' AND name IS NOT NULL ORDER BY name"
    ).fetchall()
    conn.close()

    by_name = {}
    for r in rows:
        n = r['name']
        if n not in by_name:
            by_name[n] = {'name': n, 'done': 0, 'planned': 0}
        if r['status'] == 'done':
            by_name[n]['done'] += 1
        else:
            by_name[n]['planned'] += 1

    # Total segments per street name from cache
    total_by_name = {}
    gj = load_cache()
    if gj:
        for feat in gj.get('features', []):
            n = feat['properties'].get('name')
            if n:
                total_by_name[n] = total_by_name.get(n, 0) + 1

    result = []
    for n, data in by_name.items():
        total  = total_by_name.get(n, data['done'] + data['planned'])
        marked = data['done'] + data['planned']
        pct    = round(marked / total * 100) if total > 0 else 0
        result.append({
            'name':             n,
            'status':           'done' if data['planned'] == 0 else 'planned',
            'segments_done':    data['done'],
            'segments_planned': data['planned'],
            'segments_total':   total,
            'coverage_pct':     pct,
        })

    return jsonify(sorted(result, key=lambda x: x['name']))

# --- Manual streets (kept for backwards compat) ---
@app.route('/api/manual-streets', methods=['GET'])
def api_manual_get():
    conn = get_db()
    rows = conn.execute('SELECT * FROM manual_streets ORDER BY created_at DESC').fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/manual-streets', methods=['POST'])
def api_manual_post():
    d    = request.get_json()
    name = d.get('name', '').strip()
    conn = get_db()
    conn.execute('''
        INSERT INTO manual_streets (name, house_from, house_to, status, date_done)
        VALUES (?,?,?,?,?)
    ''', (name, d.get('house_from'), d.get('house_to'),
          d.get('status', 'planned'), d.get('date_done')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/manual-streets/<int:sid>', methods=['DELETE'])
def api_manual_delete(sid):
    conn = get_db()
    conn.execute('DELETE FROM manual_streets WHERE id=?', (sid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# --- Stats ---
@app.route('/api/stats')
def api_stats():
    total          = 0
    total_segments = 0
    gj = load_cache()
    if gj:
        names          = {f['properties']['name'] for f in gj.get('features', [])}
        total          = len(names)
        total_segments = len(gj.get('features', []))

    conn = get_db()
    done    = conn.execute("SELECT COUNT(DISTINCT name) FROM streets WHERE status='done' AND name IS NOT NULL").fetchone()[0]
    planned = conn.execute("SELECT COUNT(DISTINCT name) FROM streets WHERE status='planned' AND name IS NOT NULL").fetchone()[0]
    done_segs = conn.execute("SELECT COUNT(*) FROM streets WHERE status='done'").fetchone()[0]
    conn.close()

    seg_pct = round(done_segs / total_segments * 100) if total_segments > 0 else 0

    return jsonify({
        'total':                total,
        'done':                 done,
        'planned':              planned,
        'segment_coverage_pct': seg_pct,
    })

# --- Cache refresh ---
@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    for p in [CACHE_PATH, BBOX_FILE]:
        if os.path.exists(p):
            os.remove(p)
    return jsonify({'ok': True})

# --- Reset all street statuses ---
@app.route('/api/reset-streets', methods=['POST'])
def api_reset_streets():
    conn = get_db()
    conn.execute("DELETE FROM streets")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

# --- Version ---
@app.route('/api/version')
def api_version():
    return jsonify({'version': VERSION})

# ---------------------------------------------------------------------------

def ensure_ssl_cert():
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return (CERT_FILE, KEY_FILE)
    try:
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        now  = datetime.now(timezone.utc)
        key  = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'flyertracker')])
        cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=3650))
            .sign(key, hashes.SHA256()))
        with open(KEY_FILE, 'wb') as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        with open(CERT_FILE, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        return (CERT_FILE, KEY_FILE)
    except Exception as e:
        print(f'[WARN] SSL cert generation failed ({e}), running over HTTP (GPS will not work on mobile)')
        return None

if __name__ == '__main__':
    init_db()
    ssl_context = ensure_ssl_cert()
    if ssl_context:
        print('Starting on https://0.0.0.0:8099')
        app.run(host='0.0.0.0', port=8099, debug=False, threaded=True, ssl_context=ssl_context)
    else:
        print('Starting on http://0.0.0.0:8099')
        app.run(host='0.0.0.0', port=8099, debug=False, threaded=True)
