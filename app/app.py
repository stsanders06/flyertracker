from flask import Flask, jsonify, request, render_template_string
import sqlite3
import requests
import json
import os
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

HEADERS = {'User-Agent': 'HogedrukVenlo/1.0 (flyertracker)'}

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
                 'ALTER TABLE streets ADD COLUMN house_to INTEGER']:
        try:
            conn.execute(stmt)
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
    return {'type': 'FeatureCollection', 'features': features}

def get_streets_geojson():
    # Invalidate caches if DEFAULT_BBOX changed
    cached_bbox = ''
    if os.path.exists(BBOX_FILE):
        try:
            cached_bbox = open(BBOX_FILE).read().strip()
        except Exception:
            pass
    if cached_bbox != DEFAULT_BBOX:
        if os.path.exists(CACHE_PATH):
            os.remove(CACHE_PATH)

    if not os.path.exists(CACHE_PATH):
        data = fetch_from_overpass(DEFAULT_BBOX)
        gj   = overpass_to_geojson(data)
        with open(CACHE_PATH, 'w') as f:
            json.dump(gj, f)
        with open(BBOX_FILE, 'w') as f:
            f.write(DEFAULT_BBOX)
        return gj

    with open(CACHE_PATH) as f:
        return json.load(f)

def find_street_in_cache(name):
    """Return the first matching GeoJSON feature from the cache by street name."""
    if not os.path.exists(CACHE_PATH):
        return None
    with open(CACHE_PATH) as f:
        gj = json.load(f)
    name_lower = name.lower()
    for feat in gj.get('features', []):
        if feat['properties'].get('name', '').lower() == name_lower:
            return feat['geometry']
    return None

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Flyer Tracker – Hogedruk Venlo</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;flex-direction:column;height:100vh;height:100dvh;background:#f4f4f4;overflow:hidden}

/* Header */
#hdr{background:#1565c0;color:#fff;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;z-index:100}
#hdr h1{font-size:14px;font-weight:700}

/* Content */
#content{flex:1;overflow:hidden;position:relative}
.tab{display:none;height:100%;flex-direction:column}
.tab.on{display:flex}

/* Bottom nav */
#nav{display:flex;background:#fff;border-top:1px solid #e0e0e0;flex-shrink:0}
.nb{flex:1;padding:9px 4px 7px;border:none;background:none;cursor:pointer;font-size:10px;color:#999;display:flex;flex-direction:column;align-items:center;gap:3px}
.nb .ic{font-size:21px;line-height:1}
.nb.on{color:#1565c0}

/* Search bar (map tab) */
#mapsearch{padding:9px 10px;background:#fff;border-bottom:1px solid #e5e5e5;display:flex;gap:8px;flex-shrink:0}
#area-in{flex:1;padding:8px 11px;border:1px solid #ddd;border-radius:8px;font-size:14px;outline:none}
#area-in:focus{border-color:#1565c0}
.btnp{padding:8px 14px;background:#1565c0;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.btnp:active{filter:brightness(.88)}

/* Map */
#map{flex:1}

/* GPS button */
#gpsbtn{position:fixed;bottom:76px;left:10px;width:42px;height:42px;background:#fff;border:2px solid #ccc;border-radius:50%;font-size:19px;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.2);z-index:1000}
#gpsbtn.on{border-color:#1565c0}

/* Legend */
#legend{position:fixed;bottom:70px;right:8px;background:#fff;border-radius:9px;padding:7px 11px;box-shadow:0 2px 10px rgba(0,0,0,.18);z-index:1000;font-size:11px;line-height:1.9}
#legend div{display:flex;align-items:center;gap:7px}
.ll{width:17px;height:3px;border-radius:2px;flex-shrink:0}

/* Street popup */
#popup{position:fixed;bottom:0;left:0;right:0;background:#fff;border-radius:16px 16px 0 0;padding:17px 18px 28px;box-shadow:0 -4px 20px rgba(0,0,0,.15);z-index:2000;display:none}
#popup-name{font-size:16px;font-weight:700;margin-bottom:3px}
#popup-cur{font-size:12px;color:#888;margin-bottom:14px}
#xpop{position:absolute;top:13px;right:16px;font-size:21px;color:#bbb;cursor:pointer}
.brow{display:flex;gap:8px}
.btn{flex:1;padding:11px 6px;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer}
.btn:active{filter:brightness(.88)}
.bpl{background:#f59e0b;color:#fff}
.bdo{background:#16a34a;color:#fff}
.bno{background:#e5e7eb;color:#555}

/* Loading */
#loading{position:fixed;inset:0;background:rgba(255,255,255,.93);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:5000}
#loading p{margin-top:13px;font-size:14px;color:#444}
#loading small{margin-top:5px;font-size:12px;color:#aaa}
.spin{width:38px;height:38px;border:4px solid #e5e5e5;border-top-color:#1565c0;border-radius:50%;animation:sp .75s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}

/* Tab 2 – Straten */
#streets-scroll{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.card{background:#fff;border-radius:12px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.card h3{font-size:13px;font-weight:700;color:#333;margin-bottom:11px}
.frow{display:flex;gap:8px;margin-bottom:9px}
.fcol{display:flex;flex-direction:column;gap:3px;flex:1}
.fcol label{font-size:10px;color:#aaa;font-weight:600;letter-spacing:.3px}
.fcol input,.fcol select{padding:8px 10px;border:1px solid #e0e0e0;border-radius:8px;font-size:14px;outline:none;width:100%;background:#fff}
.fcol input:focus,.fcol select:focus{border-color:#1565c0}
.si{border:1px solid #f0f0f0;border-radius:10px;padding:11px;margin-bottom:7px;display:flex;align-items:flex-start;gap:8px}
.si:last-child{margin-bottom:0}
.sinfo{flex:1}
.sname{font-weight:700;font-size:14px;color:#222}
.smeta{font-size:11px;color:#999;margin-top:2px}
.sbadge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;flex-shrink:0;white-space:nowrap}
.bpl2{background:#fef3c7;color:#92400e}
.bdo2{background:#dcfce7;color:#166534}
.delbtn{background:none;border:none;color:#ccc;font-size:18px;cursor:pointer;padding:0 3px;flex-shrink:0}
.delbtn:hover{color:#ef4444}
.empty{color:#bbb;font-size:13px;text-align:center;padding:18px}

/* Tab 3 – Stats */
#stats-scroll{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.stcard{background:#fff;border-radius:12px;padding:15px;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.sttitle{font-size:11px;color:#aaa;font-weight:600;margin-bottom:5px;letter-spacing:.3px}
.stval{font-size:28px;font-weight:800}
.stsub{font-size:12px;color:#bbb;margin-top:2px}
.pbar{height:10px;background:#f0f0f0;border-radius:5px;overflow:hidden;margin-top:10px}
.pfill{height:100%;background:#16a34a;border-radius:5px;transition:width .5s}
.sgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sgrid .stcard{margin:0}
</style>
</head>
<body>

<div id="hdr"><h1>📋 Flyer Tracker – Hogedruk Venlo</h1></div>

<div id="content">

  <!-- TAB 1: KAART -->
  <div class="tab on" id="tab-map">
    <div id="map"></div>
  </div>

  <!-- TAB 2: STRATEN -->
  <div class="tab" id="tab-streets">
    <div id="streets-scroll">
      <div class="card">
        <h3>➕ Straat toevoegen</h3>
        <div class="frow">
          <div class="fcol" style="flex:2">
            <label>STRAATNAAM</label>
            <input id="s-name" type="text" placeholder="Kerkstraat">
          </div>
          <div class="fcol">
            <label>STATUS</label>
            <select id="s-status">
              <option value="planned">📌 Gepland</option>
              <option value="done">✅ Gedaan</option>
            </select>
          </div>
        </div>
        <div class="frow">
          <div class="fcol">
            <label>VAN NR.</label>
            <input id="s-from" type="number" placeholder="1" min="1">
          </div>
          <div class="fcol">
            <label>TOT NR.</label>
            <input id="s-to" type="number" placeholder="50" min="1">
          </div>
          <div class="fcol">
            <label>DATUM</label>
            <input id="s-date" type="date">
          </div>
        </div>
        <button class="btnp" style="width:100%;padding:11px" onclick="addStreet()">Toevoegen</button>
      </div>

      <div class="card">
        <h3>📋 Overzicht</h3>
        <div id="street-list"><p class="empty">Nog geen straten toegevoegd</p></div>
      </div>
    </div>
  </div>

  <!-- TAB 3: STATISTIEKEN -->
  <div class="tab" id="tab-stats">
    <div id="stats-scroll">
      <div class="stcard">
        <div class="sttitle">VOORTGANG KAARTSTRATEN</div>
        <div class="stval" id="st-pct" style="color:#1565c0">–%</div>
        <div class="stsub" id="st-pct-sub">–</div>
        <div class="pbar"><div class="pfill" id="st-bar" style="width:0%"></div></div>
      </div>
      <div class="sgrid">
        <div class="stcard">
          <div class="sttitle">GEDAAN</div>
          <div class="stval" id="st-done" style="color:#16a34a">–</div>
          <div class="stsub">straten</div>
        </div>
        <div class="stcard">
          <div class="sttitle">GEPLAND</div>
          <div class="stval" id="st-pl" style="color:#f59e0b">–</div>
          <div class="stsub">straten</div>
        </div>
        <div class="stcard">
          <div class="sttitle">TOTAAL IN GEBIED</div>
          <div class="stval" id="st-tot" style="color:#1565c0">–</div>
          <div class="stsub">straten</div>
        </div>
        <div class="stcard">
          <div class="sttitle">HUIZEN GESCHAT</div>
          <div class="stval" id="st-huis" style="color:#7c3aed">–</div>
          <div class="stsub">handmatig ingevoerd</div>
        </div>
      </div>
      <div class="stcard">
        <div class="sttitle">HANDMATIG TOEGEVOEGD</div>
        <div class="stval" id="st-man" style="color:#333">–</div>
        <div class="stsub" id="st-man-sub">– gepland &nbsp;·&nbsp; – gedaan</div>
      </div>
    </div>
  </div>

</div>

<!-- Bottom nav -->
<div id="nav">
  <button class="nb on" id="nb-map"     onclick="goTab('map')">    <span class="ic">🗺</span>Kaart</button>
  <button class="nb"    id="nb-streets" onclick="goTab('streets')"><span class="ic">📋</span>Straten</button>
  <button class="nb"    id="nb-stats"   onclick="goTab('stats')">  <span class="ic">📊</span>Statistieken</button>
</div>

<!-- GPS btn -->
<button id="gpsbtn" onclick="toggleGPS()">📍</button>

<!-- Legend -->
<div id="legend">
  <div><span class="ll" style="background:#aaa"></span>Niet gedaan</div>
  <div><span class="ll" style="background:#f59e0b"></span>Gepland</div>
  <div><span class="ll" style="background:#16a34a"></span>Gedaan</div>
  <div><span class="ll" style="background:#16a34a;opacity:.5;border:1px dashed #16a34a"></span>Handmatig</div>
</div>

<!-- Popup -->
<div id="popup">
  <span id="xpop" onclick="closePop()">✕</span>
  <div id="popup-name"></div>
  <div id="popup-cur"></div>
  <div class="frow" style="margin:12px 0 14px">
    <div class="fcol">
      <label style="font-size:10px;color:#aaa;font-weight:600">VAN NR.</label>
      <input id="popup-from" type="number" placeholder="1" min="1">
    </div>
    <div class="fcol">
      <label style="font-size:10px;color:#aaa;font-weight:600">TOT NR.</label>
      <input id="popup-to" type="number" placeholder="50" min="1">
    </div>
  </div>
  <div class="brow">
    <button class="btn bpl" onclick="setStatus('planned')">📌 Plannen</button>
    <button class="btn bdo" onclick="setStatus('done')">✅ Gedaan</button>
    <button class="btn bno" onclick="setStatus('none')">✖ Reset</button>
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
  if (t === 'streets') loadStreetList();
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

let sLayers   = {};   // osm_id → {vis, hit}
let statuses  = {};   // osm_id → status string
let houseNums = {};   // osm_id → {from, to}
let segsByName = {}; // street name → [osm_id, ...]
let selId     = null;
let selName   = null;
let mLayers   = {};   // manual id → layer
let locMark   = null;
let watchId   = null;

// --- statuses ---
async function loadStatuses() {
  const r   = await fetch('/api/status');
  const raw = await r.json();
  statuses  = {};
  houseNums = {};
  for (const [id, val] of Object.entries(raw)) {
    statuses[id]  = val.status;
    houseNums[id] = { from: val.house_from, to: val.house_to };
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
  const pct   = total > 1 ? ` · ${Math.round((done+plan)/total*100)}% gedekt` : '';
  document.getElementById('popup-cur').textContent =
    'Status: ' + LBL[statuses[id] || 'none'] + (total > 1 ? ` (${done+plan}/${total} segm.${pct})` : '');
  const nums = houseNums[id] || {};
  document.getElementById('popup-from').value = nums.from || '';
  document.getElementById('popup-to').value   = nums.to   || '';
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
  const from = document.getElementById('popup-from').value || null;
  const to   = document.getElementById('popup-to').value   || null;
  closePop();
  await fetch('/api/status', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id, name, status:s, house_from:from ? +from : null, house_to:to ? +to : null})
  });
  statuses[id]  = s;
  houseNums[id] = { from: from ? +from : null, to: to ? +to : null };
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
    sLayers    = {};
    segsByName = {};

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
      `<p style="color:red;padding:20px">Fout: ${err.message}</p>`;
  }
}

// --- GPS ---
function toggleGPS() {
  const btn = document.getElementById('gpsbtn');
  if (watchId) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
    if (locMark) { map.removeLayer(locMark); locMark = null; }
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
    }, err => {
      const msg = err.code === 1
        ? 'Locatietoegang geweigerd. Gebruik HTTPS of open de app op hetzelfde apparaat via localhost.'
        : err.code === 2
          ? 'Locatie niet beschikbaar.'
          : 'Locatie time-out.';
      alert(msg);
      btn.classList.remove('on');
      watchId = null;
    }, { enableHighAccuracy:true });
  }
}

// --- loading helpers ---
function showLoad(msg, sub) {
  document.getElementById('load-msg').textContent = msg;
  document.getElementById('load-sub').textContent = sub;
  document.getElementById('loading').style.display = 'flex';
}
function hideLoad() { document.getElementById('loading').style.display = 'none'; }

// ============================================================
// STREETS TAB (TAB 2)
// ============================================================
async function loadStreetList() {
  const [markedRes, manualRes] = await Promise.all([
    fetch('/api/streets-marked'),
    fetch('/api/manual-streets')
  ]);
  const marked = await markedRes.json();
  const manual = await manualRes.json();
  const list   = document.getElementById('street-list');

  if (!marked.length && !manual.length) {
    list.innerHTML = '<p class="empty">Nog geen straten toegevoegd</p>';
    drawManual([]);
    return;
  }

  const markedHtml = marked.map(s => `
    <div class="si">
      <div class="sinfo">
        <div class="sname">🗺 ${s.name || '–'}</div>
        <div class="smeta">${s.house_from && s.house_to ? `Nr. ${s.house_from}–${s.house_to}` : 'Geen huisnummers'}</div>
      </div>
      <span class="sbadge ${s.status==='done'?'bdo2':'bpl2'}">
        ${s.status==='done'?'✅ Gedaan':'📌 Gepland'}
      </span>
      <button class="delbtn" onclick="resetStreet('${s.osm_id}')">×</button>
    </div>
  `).join('');

  const manualHtml = manual.map(s => `
    <div class="si">
      <div class="sinfo">
        <div class="sname">${s.name}</div>
        <div class="smeta">
          ${s.house_from && s.house_to ? `Nr. ${s.house_from}–${s.house_to}` : 'Geen huisnummers'}
          ${s.date_done ? ` · ${s.date_done}` : ''}
        </div>
      </div>
      <span class="sbadge ${s.status==='done'?'bdo2':'bpl2'}">
        ${s.status==='done'?'✅ Gedaan':'📌 Gepland'}
      </span>
      <button class="delbtn" onclick="delStreet(${s.id})">×</button>
    </div>
  `).join('');

  list.innerHTML = markedHtml + manualHtml;
  drawManual(manual);
}

async function resetStreet(osmId) {
  await fetch('/api/status', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:osmId, status:'none', house_from:null, house_to:null})
  });
  statuses[osmId]  = 'none';
  houseNums[osmId] = {};
  applyStyle(osmId);
  loadStreetList();
}

function drawManual(streets) {
  Object.values(mLayers).forEach(l => map.removeLayer(l));
  mLayers = {};
  streets.forEach(s => {
    if (!s.geometry) return;
    try {
      const layer = L.geoJSON(JSON.parse(s.geometry), {
        style:{ color:COL[s.status]||'#aaa', weight:5, opacity:.85, dashArray:'7,5' }
      }).addTo(map);
      mLayers[s.id] = layer;
    } catch(e) {}
  });
}

async function addStreet() {
  const name   = document.getElementById('s-name').value.trim();
  const from   = document.getElementById('s-from').value || null;
  const to     = document.getElementById('s-to').value   || null;
  const status = document.getElementById('s-status').value;
  const dt     = document.getElementById('s-date').value || (status==='done' ? new Date().toISOString().split('T')[0] : null);

  if (!name) { alert('Vul een straatnaam in'); return; }

  await fetch('/api/manual-streets', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ name, house_from:from, house_to:to, status, date_done:dt })
  });

  ['s-name','s-from','s-to','s-date'].forEach(id => document.getElementById(id).value = '');
  loadStreetList();
}

async function delStreet(id) {
  if (!confirm('Straat verwijderen?')) return;
  await fetch('/api/manual-streets/' + id, { method:'DELETE' });
  loadStreetList();
}

// ============================================================
// STATS (TAB 3)
// ============================================================
async function loadStats() {
  const r = await fetch('/api/stats');
  const s = await r.json();
  const pct = s.total > 0 ? Math.round(s.done / s.total * 100) : 0;
  document.getElementById('st-pct').textContent     = pct + '%';
  document.getElementById('st-pct-sub').textContent = `${s.done} van ${s.total} straten gedaan`;
  document.getElementById('st-bar').style.width     = pct + '%';
  document.getElementById('st-done').textContent    = s.done;
  document.getElementById('st-pl').textContent      = s.planned;
  document.getElementById('st-tot').textContent     = s.total;
  document.getElementById('st-huis').textContent    = s.estimated_houses ?? '–';
  document.getElementById('st-man').textContent     = s.manual_total;
  document.getElementById('st-man-sub').textContent =
    `${s.manual_planned} gepland · ${s.manual_done} gedaan`;
}

// ============================================================
// INIT
// ============================================================
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
    return render_template_string(HTML)

# --- OSM streets ---
@app.route('/api/streets')
def api_streets():
    try:
        return jsonify(get_streets_geojson())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

@app.route('/api/streets-marked')
def api_streets_marked():
    conn = get_db()
    rows = conn.execute(
        "SELECT osm_id, name, status, house_from, house_to FROM streets WHERE status != 'none' ORDER BY name"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# --- Manual streets ---
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

    # Try to find geometry in cache
    geom = find_street_in_cache(name)
    geom_json = json.dumps(geom) if geom else None

    conn = get_db()
    conn.execute('''
        INSERT INTO manual_streets (name, house_from, house_to, status, date_done, geometry)
        VALUES (?,?,?,?,?,?)
    ''', (name, d.get('house_from'), d.get('house_to'),
          d.get('status', 'planned'), d.get('date_done'), geom_json))
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
    # Total unique street names in area
    total = 0
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            gj = json.load(f)
        total = len({f['properties']['name'] for f in gj.get('features', [])})

    conn = get_db()
    done    = conn.execute("SELECT COUNT(DISTINCT name) FROM streets WHERE status='done' AND name IS NOT NULL").fetchone()[0]
    planned = conn.execute("SELECT COUNT(DISTINCT name) FROM streets WHERE status='planned' AND name IS NOT NULL").fetchone()[0]

    # Manual stats
    m_done    = conn.execute("SELECT COUNT(*) FROM manual_streets WHERE status='done'").fetchone()[0]
    m_planned = conn.execute("SELECT COUNT(*) FROM manual_streets WHERE status='planned'").fetchone()[0]

    # Estimate houses from both manual and map streets
    manual_rows = conn.execute(
        'SELECT house_from, house_to FROM manual_streets WHERE house_from IS NOT NULL AND house_to IS NOT NULL'
    ).fetchall()
    map_rows = conn.execute(
        'SELECT house_from, house_to FROM streets WHERE house_from IS NOT NULL AND house_to IS NOT NULL'
    ).fetchall()
    conn.close()

    all_rows = list(manual_rows) + list(map_rows)
    estimated = sum(
        max(1, (r['house_to'] - r['house_from']) // 2 + 1)
        for r in all_rows if r['house_to'] and r['house_from'] and r['house_to'] >= r['house_from']
    )

    return jsonify({
        'total':            total,
        'done':             done,
        'planned':          planned,
        'manual_total':     m_done + m_planned,
        'manual_done':      m_done,
        'manual_planned':   m_planned,
        'estimated_houses': estimated if estimated > 0 else None
    })

# --- Cache refresh ---
@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    for p in [CACHE_PATH, BBOX_FILE]:
        if os.path.exists(p):
            os.remove(p)
    return jsonify({'ok': True})

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
    ssl_ctx = ensure_ssl_cert()
    scheme = 'https' if ssl_ctx else 'http'
    print(f'Starting on {scheme}://0.0.0.0:8099')
    app.run(host='0.0.0.0', port=8099, debug=False, ssl_context=ssl_ctx, threaded=True)
