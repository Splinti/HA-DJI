# DJI Flight Log für Home Assistant

Custom Integration, die DJI-Fly-Flugaufzeichnungen (`DJIFlightRecord_*.txt`) aus einem Ordner importiert und daraus **Sensoren**, **geo_location-Entities** und eine **native Karten-Card** in Home Assistant macht. Läuft komplett lokal; nur zum Entschlüsseln neuerer Logs wird einmalig pro Flug ein Schlüssel von DJI geholt.

```
RC 2 / Handy ──USB──▶ PC ─┬─ Sync-Skript ──SMB──────▶ /share/dji/flightrecords
                          └─ Browser-Upload (Panel) ──▶        ▲
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
  Flüge, Flugzeit, Distanz, max. Höhe, max. Geschwindigkeit, erster/letzter Flug, letzter Flug: Dauer, Distanz, max. Höhe, max. Speed, Akku Ende / verbraucht, Status (siehe Vorfälle). Je Drohne außerdem „SD-Karte frei“ (Stand Ende des letzten Flugs, Attribute `total_mb` und `full`).
  „Gemerkte Orte“ (Anzahl, Liste im Attribut `spots`). Diagnose: letzter Import, ausstehende Dateien, Anzahl Drohnen. Button „Log-Ordner scannen“ für sofortigen Import.
- **Akkus** – ein Gerät je Flugakku (erkannt an der Seriennummer im Log): Ladezyklen, Lebensdauer und Kapazität (volle gegenüber Nenn-Kapazität, laut Akku-Elektronik), Flüge und Flugzeit mit diesem Akku, dazu aus dem letzten Flug die höchste Temperatur (Starttemperatur im Attribut), die niedrigste Zellspannung und die größte Abweichung zwischen den Zellen. Ohne API-Key kennt die Integration nur die Seriennummer, also nur Flüge und Flugzeit. Beispiel-Automation für eine Akku-Warnung in [`examples/automations.yaml`](examples/automations.yaml).
- **Vorfälle** – jeder Flug bekommt einen Status `ok`, `warning` oder `critical`, je nachdem, ob der Flugcontroller selbst eingegriffen hat: Warnung z. B. bei Smart-RTH oder Landung wegen niedrigem Akku, kritisch z. B. bei Zwangslandung, RTH nach Verbindungsverlust oder blockiertem Motor. Welche Aktionen es waren, steht in `incident_actions`. Ein per Taste ausgelöstes RTH zählt nicht, schnelle Sinkflüge auch nicht (bei FPV normal). Im Panel stehen Vorfälle und eine volle SD-Karte in der Flugliste und im Popup. Braucht den API-Key.
- **geo_location** *(optional, standardmäßig aus)* – Startpunkt jedes Flugs als Entity (`source: dji_flightlog`), nutzbar auf der eingebauten Map-Card und in Zonen-Automationen. Aus gutem Grund opt-in: HA hängt an das automatische „Übersicht"-Dashboard eine Karte an, sobald *irgendeine* `geo_location`-Entity existiert – die Flüge würden dann ungefragt auf der Standard-Karte landen.
- **Eigenes Panel in der Seitenleiste** – Vollbild-Ansicht mit Statistik, Filtern, großer Karte und Flugliste zum Anklicken. Flugaufzeichnungen lassen sich dort direkt **hochladen** (Button oder Drag & Drop).
- **Orte merken mit DIPUL-Zonen** – im Panel einen Punkt auf der Karte wählen und speichern, die [DIPUL](https://www.dipul.de)-Geozonen (Flughäfen, Kontrollzonen, Naturschutz, Wohngebiete, …) werden dabei eingeblendet und am Punkt abgefragt. Liste im Dashboard per `custom:dji-spots-card`, mit Google-Maps-Link zum Starten der Navigation.
- **Karte** – `custom:dji-flight-map-card` (Leaflet, offline-fähig außer Kacheln): alle Tracks, Heatmap, Popups mit Kennzahlen und GPX/KML/GeoJSON-Download, Filter nach Zeitraum/Drohne, Modus „nur letzter Flug".
- **Event** `dji_flightlog_flight_imported` bei jedem neuen Flug (Payload = Flugzusammenfassung) → Benachrichtigung, OneDrive-Upload, …
- **Services** `dji_flightlog.scan`, `dji_flightlog.import_file`, `dji_flightlog.export_track` (GPX/KML/GeoJSON, in Datei oder als Response). Die Höhe in den Exporten ist die Höhe über dem Startpunkt (KML: `relativeToGround`). Die absolute Höhe im Log taugt nicht dafür: DJI rechnet eine barometrische Home-Höhe dazu, die von Tag zu Tag wandert und selbst auf Meereshöhe unter 0 m liegt.
- **HTTP-API** (HA-Auth): `/api/dji_flightlog/flights`, `/tracks`, `/flights/<id>/track`, `/flights/<id>/export/<gpx|kml|geojson>`, `/spots` (GET/POST), `/spots/<id>` (PATCH/DELETE), `/upload` (POST, multipart-Feld `file`, nur Admins).

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
| geo_location-Limit | 0 (aus) | Nur die neuesten N Flüge bekommen eine Entity. **0 = keine** – siehe Hinweis unten |
| In der Seitenleiste anzeigen | an | Panel „Drohnenflüge" in der HA-Seitenleiste |

**DJI API-Key** (kostenlos): auf [developer.dji.com](https://developer.dji.com) registrieren → *Developer Center* → *Create App* → Typ **Open API** → E-Mail bestätigen → *App Key* kopieren.

## Eigenes Dashboard in der Seitenleiste

Die Integration registriert beim Start ein vollwertiges Panel **„Drohnenflüge"** in der HA-Seitenleiste – kein Lovelace-Dashboard, sondern eine eigene Seite:

- Statistik-Kacheln (Flüge, Flugzeit, Strecke, max. Höhe/Speed, letzter Flug) über den gefilterten Zeitraum
- Filter: Zeitraum (7 Tage … alles), Drohne (ab zwei Drohnen), Heatmap an/aus
- **Suche** über der Karte: Postleitzahl, Ort, Adresse oder Koordinaten eingeben, Enter. Die Karte springt hin und setzt einen Marker, dessen Popup „Ort merken“ (Name schon vorbelegt) und Navigation anbietet. Koordinaten gehen in allen üblichen Schreibweisen: `48.13743, 11.57549`, `48,13743 11,57549`, `N 48.13743 E 11.57549`, `48°08'14.7"N 11°34'31.8"E` (so kopiert man sie aus Google Maps) oder ein Google-Maps-Link mit `@48.13743,11.57549`. Koordinaten werden lokal erkannt; alles andere fragt der Browser bei [Nominatim](https://nominatim.org) (OpenStreetMap) an, eine reine PLZ zuerst als Postleitzahl im Land der HA-Instanz
- Große Karte, die die volle Höhe nutzt
- Flugliste rechts (auf dem Handy darunter), nach Tagen gruppiert; Klick auf einen Flug zoomt auf ihn und hebt ihn hervor, nochmal klicken hebt die Auswahl auf
- ↻-Button oben rechts scannt den Log-Ordner sofort
- ⇧-Button oben rechts: **Flugaufzeichnungen hochladen** (nur für Admins). Alternativ Dateien oder den ganzen Ordner `FlightRecord` auf die Seite ziehen. Die Dateien landen im Log-Ordner und werden sofort importiert; schon vorhandene werden erkannt und nicht doppelt gespeichert
- Marker-Button oben rechts: **Ort merken** (siehe unten); Schalter „DIPUL-Zonen“ blendet die Geozonen auch ohne Planungsmodus ein
- Tab „Orte“ in der Liste: gemerkte Orte mit Navigations-Link und Löschen; Klick zoomt auf den Ort
- Hinweisleiste, wenn Flüge ohne GPS-Track importiert wurden (fehlender API-Key) oder nicht unterstützte Dateien im Ordner liegen

Abschaltbar über *Integration → Konfigurieren → „In der Seitenleiste anzeigen"*. Die Position in der Seitenleiste lässt sich wie bei jedem Panel per Rechtsklick bzw. über *Profil → Seitenleiste bearbeiten* ändern.

## Orte merken (DIPUL-Zonen)

Im Panel einfach auf eine freie Stelle der Karte tippen: Es öffnet sich „Neuer Ort“ mit den DIPUL-Zonen an diesem Punkt. Der Marker-Button oben rechts schaltet zusätzlich den Planungsmodus ein: Die Karte blendet die Geozonen der [DIPUL](https://www.dipul.de) (DFS, Digitale Plattform Unbemannte Luftfahrt) ein, ab Zoomstufe 8. Ein Tipp auf die Karte fragt die Zonen an diesem Punkt ab (z. B. „Kontrollzone Frankfurt Main (EDDF) Zone 4 (691 ft MSL – 2500 ft MSL)“, „Vogelschutzgebiet Hessische Rhön“). Name und Notiz eingeben, **Merken**.

- Gespeichert wird der Punkt samt der Zonen zum Zeitpunkt der Abfrage. Im Popup eines Ortes lassen sich die Zonen mit „Zonen prüfen“ aktualisieren.
- **Navigation** öffnet `https://www.google.com/maps/dir/?api=1&destination=<lat>,<lon>`, auf dem Handy also direkt die Google-Maps-App mit Route.
- Die Zonen dienen nur zur Orientierung und ersetzen keine Prüfung vor dem Flug (temporäre Beschränkungen ändern sich laufend). Die Abfrage geht direkt vom Browser an `uas-betrieb.de`. Daten: © DFS / dipul, CC BY-ND 4.0.

Für das Dashboard gibt es eine Liste der gemerkten Orte (steckt in derselben Resource wie die Karte):

```yaml
type: custom:dji-spots-card
title: Gemerkte Orte
zones: true           # DIPUL-Zonen als Chips anzeigen
limit: 10             # optional
panel_path: /dji-flightlog   # Tipp auf einen Ort öffnet ihn im Panel; null = aus
```

Der Sensor `sensor.dji_flight_log_saved_spots` enthält dieselbe Liste im Attribut `spots` (inkl. `maps_url`), z. B. für eine Markdown-Card oder eine Benachrichtigung.

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
tiles: ha          # Start-Ebene: ha (HA-eigener OSM-Proxy, Default) | carto | satellite (Esri) | topo
tile_switch: true  # Umschalter „Karte | Satellit“ oben rechts; die Wahl merkt sich der Browser
spot_on_click: false # Klick auf die Karte öffnet „Neuer Ort“ (im Panel immer an)
height: 450
```

Satellitenbilder kommen von Esri World Imagery (mit Orts- und Grenznamen darüber) und werden im Dark Mode nicht invertiert.

Weitere Optionen: `dipul` (DIPUL-Geozonen einblenden, Default false), `spots` (gemerkte Orte anzeigen, Default true bei `mode: all`), `scan_button` (↻ im Titel, Default true), `limit`, `since` (ISO-Datum), `line_color`, `line_weight`, `max_points` (Punkte pro Track in der Übersicht, Default 400), `dark` (`auto`/`true`/`false`), `refresh_entity` (Default `sensor.dji_flight_log_last_import`), `refresh_seconds`.

### Flüge auf der Standard-Karte

Setzt man das geo_location-Limit auf einen Wert > 0, zeigt auch die **eingebaute Map-Card** die Startpunkte:

```yaml
type: map
geo_location_sources:
  - dji_flightlog
```

Nebenwirkung: Home Assistant ergänzt sein automatisch erzeugtes „Übersicht"-Dashboard dann selbsttätig um eine Karte mit allen `geo_location`-Quellen. Wer die Flüge dort **nicht** sehen will, lässt das Limit auf 0 – die Entities werden dann samt Registry-Einträgen entfernt.

Komplettes Beispiel-Dashboard: [`examples/dashboard.yaml`](examples/dashboard.yaml).

## Automationen

Beispiele in [`examples/automations.yaml`](examples/automations.yaml):
- Benachrichtigung bei neuem Flug
- Rohdatei + GPX nach **OneDrive** hochladen (native `onedrive.upload`-Action)
- GPX lokal unter `/share/dji/exports` ablegen
- Akku-Warnung, wenn ein Akku über 60 °C warm wurde, die Zellen mehr als 0,1 V auseinanderlagen oder eine Zelle unter 3,0 V fiel

Für `export_track` mit `path` muss der Zielordner in `allowlist_external_dirs` stehen:

```yaml
homeassistant:
  allowlist_external_dirs:
    - /share/dji
```

## Welche Datei brauche ich?

Nur die **App-Flugaufzeichnung** der DJI-Fly-App:

| Datei | Woher | Brauchbar? |
|---|---|---|
| `DJIFlightRecord_*.txt` bzw. `FlightRecord_*.txt` | RC 2 / Handy: `Android/data/dji.go.v5/files/FlightRecord/` | **Ja** – das ist die richtige (neuere DJI-Fly-Versionen lassen das `DJI`-Präfix weg) |
| `DJI_<Modell>_<Datum>.DAT` (zig MB) | DJI Assistant 2 / "Geräteprotokolle exportieren" | Nein – Werkstatt-Bundle aus AES-verschlüsselten `*.log.enc` und `FC_SMP-*.DAT.enc`; nur DJI kann das lesen |
| `FLYnnn.DAT` | SD-Karte / Flightcontroller | Nein – bei allen aktuellen Modellen verschlüsselt |

Nicht verwertbare Dateien werden nicht stillschweigend übersprungen: sie landen im Sensor „Nicht unterstützte Dateien" (mit Dateiname und Grund) und erzeugen eine erklärende Warnung im Protokoll.

## Logs vom RC 2 / Handy auf den HA-Host bekommen

Siehe [`docs/sync.md`](docs/sync.md). Kurzfassung: DJI blockt APK-Installation auf dem RC 2 und Android verbietet fremden Apps den Zugriff auf `Android/data`, deshalb läuft der Sync über USB am PC: [`scripts/Sync-DjiFlightRecords.ps1`](scripts/Sync-DjiFlightRecords.ps1) kopiert bei angestecktem Gerät neue Logs auf den HA-Samba-Share. Ohne jede Einrichtung geht es über den **Upload im Panel**: Gerät anstecken, im Explorer den Ordner `FlightRecord` öffnen, alles markieren und ins Panel ziehen.

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
- Liest eine neue Version der Integration mehr oder korrekter aus den Logs, werden bereits importierte Flüge beim nächsten Scan still neu eingelesen (kein erneutes `flight_imported`-Event). Das geht nur für Flüge, deren Rohdatei noch im Ordner liegt, und bei verschlüsselten Logs nur mit API-Key.

## Credits

- [pydjirecord](https://github.com/rembish/pydjirecord) (Port von [dji-log-parser](https://github.com/lvauvillier/dji-log-parser)) für das Log-Format inkl. Entschlüsselung
- [Leaflet](https://leafletjs.com) + [leaflet.heat](https://github.com/Leaflet/Leaflet.heat) (vendored unter `custom_components/dji_flightlog/www`)
