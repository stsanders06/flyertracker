# Flyer Tracker – Hogedruk Venlo

Interactieve kaart om bij te houden welke straten je al geflyerd hebt.
Draait als Home Assistant add-on op je Raspberry Pi.

---

## Installatie

### 1. Bestanden op de Pi zetten

Zet de `flyertracker` map in de `addons` map op je Pi.
Gebruik hiervoor de **Samba share** add-on of **SSH** add-on.

Pad: `/addons/flyertracker/`

Structuur moet er zo uitzien:
```
/addons/flyertracker/
├── config.yaml
├── Dockerfile
└── app/
    └── app.py
```

### 2. Add-on laden in Home Assistant

1. Ga naar **Instellingen → Add-ons → Add-on store**
2. Klik rechtsboven op de ⋮ (drie puntjes)
3. Klik **Check for updates** of **Reload**
4. Scroll omlaag naar **Local add-ons**
5. Je ziet nu **Flyer Tracker** staan
6. Klik erop → **Installeren**
7. Na installatie: **Starten**

### 3. Openen

Ga in je browser naar:
```
http://homeassistant.local:8099
```

Of via het IP van je Pi:
```
http://192.168.x.x:8099
```

---

## Gebruik

- **Tik op een straat** → popup verschijnt
- **📌 Plannen** → oranje (gepland voor flyeren)
- **✅ Gedaan** → groen (al geflyerd)
- **✖ Reset** → terug naar grijs
- Bovenin zie je hoeveel straten gedaan / gepland / totaal

De straten worden de eerste keer opgehaald van OpenStreetMap (~30 seconden).
Daarna wordt alles gecached op de Pi.

---

## Aanpassen

### Ander gebied flyeren
Pas `BBOX` aan in `app/app.py` (regel 16):
```python
BBOX = "51.33,6.10,51.42,6.22"  # min_lat, min_lon, max_lat, max_lon
```

Gebruik https://boundingbox.klokantech.com om coördinaten te vinden.
Verwijder daarna de cache zodat nieuwe straten worden geladen:
```
POST http://homeassistant.local:8099/api/refresh
```
Of wis `/data/streets_cache.json` via SSH.

---

## Data

Alle data staat opgeslagen in:
- `/data/flyertracker.db` – SQLite database met straat-statussen
- `/data/streets_cache.json` – cache van OpenStreetMap straten

Maak een backup van `/data/` om je voortgang te bewaren.
