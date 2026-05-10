from flask import Flask, jsonify, request, render_template_string
import sqlite3
import requests
import json
import os

app = Flask(__name__)

DATA_DIR = '/data'
DB_PATH = os.path.join(DATA_DIR, 'flyertracker.db')
CACHE_PATH = os.path.join(DATA_DIR, 'streets_cache.json')

# Bounding box: Venlo + Tegelen
# Pas dit aan als je een ander gebied wil
BBOX = "51.33,6.10,51.42,6.22"

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
    conn.execute('''
        CREATE TABLE IF NOT EXISTS streets (
            osm_id      TEXT PRIMARY KEY,
            name        TEXT,
            status      TEXT DEFAULT 'none',
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            notes       TEXT DEFAULT ''
        )
    ''')
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Straten ophalen via Overpass API
# ---------------------------------------------------------------------------

def fetch_from_overpass():
    query = f"""
    [out:json][timeout:90];
    (
      way["highway"]["name"]({BBOX});
    );
    out geom;
    """
    resp = requests.post(
        'https://overpass-api.de/api/interpreter',
        data=query,
        timeout=120
    )
    resp.raise_for_status()
    return resp.json()

def get_streets_geojson():
    # Geef cache terug als die bestaat
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)

    # Haal op van Overpass
    data = fetch_from_overpass()

    features = []
    for element in data.get('elements', []):
        if element['type'] != 'way' or 'geometry' not in element:
            continue

        # Filter op relevante wegtypes (geen snelwegen etc.)
        highway = element.get('tags', {}).get('highway', '')
        if highway in ('motorway', 'motorway_link', 'trunk', 'trunk_link'):
            continue

        coords = [[pt['lon'], pt['lat']] for pt in element['geometry']]
        features.append({
            'type': 'Feature',
            'id': str(element['id']),
            'properties': {
                'name': element.get('tags', {}).get('name', 'Onbekend'),
                'highway': highway,
            },
            'geometry': {
                'type': 'LineString',
                'coordinates': coords
            }
        })

    geojson = {'type': 'FeatureCollection', 'features': features}

    with open(CACHE_PATH, 'w') as f:
        json.dump(geojson, f)

    return geojson

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML = """
<!DOCTYPE html>
<html lang="nl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
    <title>Flyer Tracker – Hogedruk Venlo</title>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            display: flex;
            flex-direction: column;
            height: 100vh;
            background: #f0f0f0;
        }

        /* Header */
        #header {
            background: #1565c0;
            color: white;
            padding: 10px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-shrink: 0;
            gap: 8px;
        }
        #header h1 { font-size: 15px; font-weight: 700; }
        #stats { font-size: 12px; opacity: 0.85; white-space: nowrap; }

        /* Map */
        #map { flex: 1; z-index: 1; }

        /* Legenda */
        #legend {
            position: fixed;
            bottom: 20px;
            right: 10px;
            background: white;
            border-radius: 10px;
            padding: 10px 14px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.25);
            z-index: 1000;
            font-size: 12px;
            line-height: 1.8;
        }
        #legend div { display: flex; align-items: center; gap: 8px; }
        .leg-line {
            width: 20px; height: 4px; border-radius: 2px; flex-shrink: 0;
        }

        /* Loading overlay */
        #loading {
            position: fixed;
            inset: 0;
            background: rgba(255,255,255,0.92);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            z-index: 3000;
        }
        #loading p { margin-top: 14px; font-size: 14px; color: #444; }
        #loading small { margin-top: 6px; font-size: 12px; color: #888; }
        .spinner {
            width: 40px; height: 40px;
            border: 4px solid #e0e0e0;
            border-top-color: #1565c0;
            border-radius: 50%;
            animation: spin 0.75s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* Popup panel */
        #popup {
            position: fixed;
            bottom: 0;
            left: 0; right: 0;
            background: white;
            border-radius: 16px 16px 0 0;
            padding: 18px 20px 28px;
            box-shadow: 0 -4px 20px rgba(0,0,0,0.15);
            z-index: 2000;
            display: none;
            transition: transform 0.2s ease;
        }
        #popup-name {
            font-size: 17px;
            font-weight: 700;
            margin-bottom: 6px;
            color: #111;
        }
        #popup-current {
            font-size: 13px;
            color: #666;
            margin-bottom: 16px;
        }
        .btn-row {
            display: flex;
            gap: 10px;
        }
        .btn {
            flex: 1;
            padding: 12px 8px;
            border: none;
            border-radius: 10px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            letter-spacing: 0.2px;
        }
        .btn-planned { background: #f59e0b; color: white; }
        .btn-done    { background: #16a34a; color: white; }
        .btn-none    { background: #e5e7eb; color: #555; }
        .btn:active  { filter: brightness(0.9); }

        #close-popup {
            position: absolute;
            top: 14px; right: 18px;
            font-size: 22px;
            color: #aaa;
            cursor: pointer;
            line-height: 1;
        }
    </style>
</head>
<body>

<div id="header">
    <h1>📋 Flyer Tracker</h1>
    <div id="stats">Laden…</div>
</div>

<div id="map"></div>

<div id="loading">
    <div class="spinner"></div>
    <p>Straten ophalen…</p>
    <small>Eerste keer duurt even (~30s)</small>
</div>

<div id="legend">
    <div><span class="leg-line" style="background:#aaa"></span> Niet gedaan</div>
    <div><span class="leg-line" style="background:#f59e0b"></span> Gepland</div>
    <div><span class="leg-line" style="background:#16a34a"></span> Gedaan</div>
</div>

<div id="popup">
    <span id="close-popup" onclick="closePopup()">✕</span>
    <div id="popup-name"></div>
    <div id="popup-current"></div>
    <div class="btn-row">
        <button class="btn btn-planned" onclick="setStatus('planned')">📌 Plannen</button>
        <button class="btn btn-done"    onclick="setStatus('done')">✅ Gedaan</button>
        <button class="btn btn-none"    onclick="setStatus('none')">✖ Reset</button>
    </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const STATUS_COLORS = { none: '#aaaaaa', planned: '#f59e0b', done: '#16a34a' };
const STATUS_WEIGHT = { none: 3, planned: 5, done: 5 };
const STATUS_OPACITY = { none: 0.45, planned: 1, done: 1 };
const STATUS_LABELS = { none: 'Niet gedaan', planned: 'Gepland', done: 'Gedaan' };

const map = L.map('map').setView([51.370, 6.168], 14);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap',
    maxZoom: 19
}).addTo(map);

let streetLayers = {};   // id → L.geoJSON layer
let statuses = {};       // id → 'none' | 'planned' | 'done'
let selectedId = null;

// ---------- Stats ----------
function updateStats() {
    const total   = Object.keys(streetLayers).length;
    const done    = Object.values(statuses).filter(s => s === 'done').length;
    const planned = Object.values(statuses).filter(s => s === 'planned').length;
    document.getElementById('stats').textContent =
        `✅ ${done}  📌 ${planned}  van ${total} straten`;
}

// ---------- Statussen laden ----------
async function loadStatuses() {
    const resp = await fetch('/api/status');
    statuses = await resp.json();
}

// ---------- Layer stylen ----------
function applyStyle(id) {
    const s = statuses[id] || 'none';
    if (streetLayers[id]) {
        streetLayers[id].setStyle({
            color:   STATUS_COLORS[s],
            weight:  STATUS_WEIGHT[s],
            opacity: STATUS_OPACITY[s]
        });
    }
}

// ---------- Popup ----------
function openPopup(id, name) {
    selectedId = id;
    document.getElementById('popup-name').textContent = name;
    document.getElementById('popup-current').textContent =
        'Huidige status: ' + STATUS_LABELS[statuses[id] || 'none'];
    document.getElementById('popup').style.display = 'block';
}

function closePopup() {
    document.getElementById('popup').style.display = 'none';
    selectedId = null;
}

map.on('click', closePopup);

// ---------- Status opslaan ----------
async function setStatus(status) {
    if (!selectedId) return;
    const id = selectedId;
    closePopup();

    await fetch('/api/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, status })
    });

    statuses[id] = status;
    applyStyle(id);
    updateStats();
}

// ---------- Alles laden ----------
async function init() {
    await loadStatuses();

    const resp = await fetch('/api/streets');
    const geojson = await resp.json();

    document.getElementById('loading').style.display = 'none';

    geojson.features.forEach(feature => {
        const id   = feature.id;
        const name = feature.properties.name || 'Onbekend';
        const s    = statuses[id] || 'none';

        const layer = L.geoJSON(feature, {
            style: {
                color:   STATUS_COLORS[s],
                weight:  STATUS_WEIGHT[s],
                opacity: STATUS_OPACITY[s]
            }
        });

        layer.on('click', e => {
            L.DomEvent.stopPropagation(e);
            openPopup(id, name);
        });

        layer.addTo(map);
        streetLayers[id] = layer;
    });

    updateStats();
}

init().catch(err => {
    document.getElementById('loading').innerHTML =
        '<p style="color:red;padding:20px">Fout bij laden: ' + err.message + '</p>';
});
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

@app.route('/api/streets')
def api_streets():
    try:
        return jsonify(get_streets_geojson())
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/status', methods=['GET'])
def api_status_get():
    conn = get_db()
    rows = conn.execute('SELECT osm_id, status FROM streets').fetchall()
    conn.close()
    return jsonify({row['osm_id']: row['status'] for row in rows})

@app.route('/api/status', methods=['POST'])
def api_status_post():
    data = request.get_json()
    conn = get_db()
    conn.execute('''
        INSERT INTO streets (osm_id, status)
        VALUES (?, ?)
        ON CONFLICT(osm_id) DO UPDATE
          SET status=excluded.status,
              updated_at=CURRENT_TIMESTAMP
    ''', (data['id'], data['status']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Verwijder de straten-cache zodat Overpass opnieuw wordt bevraagd."""
    if os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)
    return jsonify({'ok': True, 'message': 'Cache verwijderd, herlaad de pagina'})

# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8099, debug=False)
