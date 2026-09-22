# DJI Flight Log für Home Assistant

Custom Integration, die DJI-Fly-Flugaufzeichnungen (`DJIFlightRecord_*.txt`) aus einem Ordner importiert und daraus **Sensoren**, **geo_location-Entities** und eine **native Karten-Card** in Home Assistant macht. Läuft komplett lokal; nur zum Entschlüsseln neuerer Logs wird einmalig pro Flug ein Schlüssel von DJI geholt.

```
RC 2 / Handy ──USB──▶ PC (Sync-Skript) ──SMB──▶ /share/dji/flightrecords
                                                        │
                                          dji_flightlog (Watch-Folder, pydjirecord)
                                                        │
              ┌─────────────────┬───────────────────────┼─────────────────────┐
              ▼                 ▼                       ▼                     ▼
   Sensoren je Drohne     geo_location je Flug    dji-flight-map-card    Event + Services
   (Flüge, Zeit, Höhe…)   (native Map-Card)       (Tracks, Heatmap)      (OneDrive, GPX…)
```

## Features

- **Sensoren** – ein Gerät „DJI Flight Log" mit Gesamtwerten plus ein Gerät je Drohne (Seriennummer):
  Flüge, Flugzeit, Distanz, max. Höhe, max. Geschwindigkeit, erster/letzter Flug, letzter Flug: Dauer, Distanz, max. Höhe, max. Speed, Akku Ende / verbraucht.
  Diagnose: letzter Import, ausstehende Dateien, Anzahl Drohnen.
- **geo_location** – Startpunkt jedes Flugs als Entity (`source: dji_flightlog`) → erscheint auf der eingebauten Map-Card, nutzbar in Zonen-Automationen.
- **Karte** – `custom:dji-flight-map-card` (Leaflet, offline-fähig außer Kacheln): alle Tracks, Heatmap, Popups mit Kennzahlen und GPX/KML/GeoJSON-Download, Filter nach Zeitraum/Drohne, Modus „nur letzter Flug".
- **Event** `dji_flightlog_flight_imported` bei jedem neuen Flug (Payload = Flugzusammenfassung) → Benachrichtigung, OneDrive-Upload, …
- **Services** `dji_flightlog.scan`, `dji_flightlog.import_file`, `dji_flightlog.export_track` (GPX/KML/GeoJSON, in Datei oder als Response).
- **HTTP-API** (HA-Auth): `/api/dji_flightlog/flights`, `/tracks`, `/flights/<id>/track`, `/flights/<id>/export/<gpx|kml|geojson>`.

## Installation

### HACS (empfohlen)
1. HACS → Integrationen → ⋮ → *Benutzerdefinierte Repositories* → dieses Repo als Typ *Integration* hinzufügen.
2. „DJI Flight Log" installieren, HA neu starten.

### Manuell
`custom_components/dji_flightlog` nach `/config/custom_components/` kopieren, HA neu starten.

### Einrichten
*Einstellungen → Geräte & Dienste → Integration hinzufügen → DJI Flight Log*

| Option | Default | Bedeutung |
|---|---|---|
| Log-Ordner | `/share/dji/flightrecords` | Wird rekursiv gescannt; muss existieren |
| DJI API-Key | – | Nötig für GPS-Track/Telemetrie bei Logs ab v13 (alle aktuellen Drohnen). Ohne Key: nur Header-Daten (Zeit, Dauer, Distanz, max. Höhe) |
| Scan-Intervall | 300 s | |
| Max. Punkte pro Track | 1500 | Downsampling beim Speichern |
| geo_location-Limit | 200 | Nur die neuesten N Flüge bekommen eine Entity |

**DJI API-Key** (kostenlos): auf [developer.dji.com](https://developer.dji.com) registrieren → *Developer Center* → *Create App* → Typ **Open API** → E-Mail bestätigen → *App Key* kopieren.

## Karte im Dashboard

Die Card-Resource wird beim Start automatisch registriert (Dashboards im Storage-Modus). Im YAML-Modus manuell eintragen:

```yaml
resources:
  - url: /dji_flightlog_static/dji-flight-map-card.js
    type: module
```

```yaml
type: custom:dji-flight-map-card
title: Drohnenflüge
mode: all          # all | last | flight (mit flight_id)
days: 365          # optional: nur die letzten N Tage
aircraft: Neo      # optional: Name oder Seriennummer
heatmap: true
markers: true      # Startpunkte
home: true         # Home-Punkte
tiles: osm         # osm | satellite | topo
height: 450
```

Weitere Optionen: `limit`, `since` (ISO-Datum), `line_color`, `line_weight`, `max_points` (Punkte pro Track in der Übersicht, Default 400), `dark` (`auto`/`true`/`false`), `refresh_entity` (Default `sensor.dji_flight_log_last_import`), `refresh_seconds`.

Die **eingebaute Map-Card** zeigt die Startpunkte ohne Zusatz-Card:

```yaml
type: map
geo_location_sources:
  - dji_flightlog
```

Komplettes Beispiel-Dashboard: [`examples/dashboard.yaml`](examples/dashboard.yaml).

## Automationen

Beispiele in [`examples/automations.yaml`](examples/automations.yaml):
- Benachrichtigung bei neuem Flug
- Rohdatei + GPX nach **OneDrive** hochladen (native `onedrive.upload`-Action)
- GPX lokal unter `/share/dji/exports` ablegen

Für `export_track` mit `path` muss der Zielordner in `allowlist_external_dirs` stehen:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /share/dji
```

## Logs vom RC 2 / Handy auf den HA-Host bekommen

Siehe [`docs/sync.md`](docs/sync.md). Kurzfassung: DJI blockt APK-Installation auf dem RC 2 und Android verbietet fremden Apps den Zugriff auf `Android/data`, deshalb läuft der Sync über USB am PC: [`scripts/Sync-DjiFlightRecords.ps1`](scripts/Sync-DjiFlightRecords.ps1) kopiert bei angestecktem Gerät neue Logs auf den HA-Samba-Share.

## Entwicklung

```powershell
python -m venv venv; .\venv\Scripts\pip install homeassistant pydjirecord pytest-homeassistant-custom-component ruff
.\venv\Scripts\python -m pytest
.\venv\Scripts\ruff check custom_components tests
```

Unter Windows fehlen `fcntl`/`resource`; `tests/conftest.py` enthält den nötigen Socket-Workaround, die beiden Module müssen als leere Stubs in `site-packages` liegen (siehe `docs/dev-windows.md`).

## Speicherort der Daten

- Index (Zusammenfassungen, Datei-Bookkeeping): `/config/.storage/dji_flightlog.flights`
- Tracks: `/config/.storage/dji_flightlog/tracks/<flight_id>.json`
- Beides ist Teil des HA-Backups. Wird eine Rohdatei aus dem Ordner gelöscht, bleibt der Flug im Logbuch.

## Credits

- [pydjirecord](https://github.com/rembish/pydjirecord) (Port von [dji-log-parser](https://github.com/lvauvillier/dji-log-parser)) für das Log-Format inkl. Entschlüsselung
- [Leaflet](https://leafletjs.com) + [leaflet.heat](https://github.com/Leaflet/Leaflet.heat) (vendored unter `custom_components/dji_flightlog/www`)
