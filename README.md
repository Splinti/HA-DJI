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
- **Eigenes Panel in der Seitenleiste** – drei Ansichten: *Flüge* (Statistik, Filter, große Karte, Flugliste), *Flug* (Details eines Flugs mit Verlaufsdiagrammen, Flugmodi, Ereignissen und Akku) und *Planen* (Karte mit DIPUL-Zonen, Suche und gemerkten Orten). Flugaufzeichnungen lassen sich dort direkt **hochladen** (Button oder Drag & Drop).
- **Piloten** – Flüge Personen zuordnen: automatisch über die Drohne (z. B. „die Avata fliegt immer Nico“) und je Flug von Hand. Ein Pilot lässt sich mit einem Home-Assistant-Benutzer verknüpfen, der beim Öffnen des Panels dann seine eigenen Flüge sieht. Filter nach Pilot im Panel und in der Karten-Card (`pilot: me`), Pilot im Event `dji_flightlog_flight_imported` (`pilot_id`, `pilot_name`).
- **Notizen** – zu jedem Flug eine eigene Notiz (Wetter, wer dabei war, was geübt wurde …), in der Flugliste und im Karten-Popup sichtbar.
- **Orte merken mit DIPUL-Zonen** – im Panel einen Punkt auf der Karte wählen und speichern, die [DIPUL](https://www.dipul.de)-Geozonen (Flughäfen, Kontrollzonen, Naturschutz, Wohngebiete, …) werden dabei eingeblendet und am Punkt abgefragt. Liste im Dashboard per `custom:dji-spots-card`, mit Google-Maps-Link zum Starten der Navigation.
- **Karte** – `custom:dji-flight-map-card` (Leaflet, offline-fähig außer Kacheln): alle Tracks, Heatmap, Popups mit Kennzahlen und GPX/KML/GeoJSON-Download, Filter nach Zeitraum/Drohne, Modus „nur letzter Flug".
- **Aufnahmen aus OneDrive oder vom NAS** *(optional)* – Videos/Fotos (inkl. 360°-`.OSV` der Avata 360) aus einem OneDrive-Ordner oder einem lokalen Ordner (auch SMB/NFS-Freigaben, die Home Assistant als Netzwerkspeicher einbindet) werden per Aufnahmezeit den Flügen zugeordnet: Vorschaubilder und Player im Panel (360°-Aufnahmen als 360°-Video), Vorschaubilder im Karten-Popup, Link zur Datei in OneDrive bzw. Download des Originals. Mehrere Konten/Ordner gleichzeitig, doppelte Aufnahmen werden zusammengeführt; auf Wunsch werden dort liegende Flugaufzeichnungen (z. B. eines zweiten Piloten) in den Log-Ordner übernommen. Siehe [`docs/onedrive.md`](docs/onedrive.md) und [`docs/local-media.md`](docs/local-media.md).
- **Event** `dji_flightlog_flight_imported` bei jedem neuen Flug (Payload = Flugzusammenfassung) → Benachrichtigung, OneDrive-Upload, …
- **Services** `dji_flightlog.scan`, `dji_flightlog.import_file`, `dji_flightlog.export_track` (GPX/KML/GeoJSON, in Datei oder als Response). Die Höhe in den Exporten ist die Höhe über dem Startpunkt (KML: `relativeToGround`). Die absolute Höhe im Log taugt nicht dafür: DJI rechnet eine barometrische Home-Höhe dazu, die von Tag zu Tag wandert und selbst auf Meereshöhe unter 0 m liegt.
- **HTTP-API** (HA-Auth): `/api/dji_flightlog/flights`, `/tracks`, `/flights/<id>/track`, `/flights/<id>/export/<gpx|kml|geojson>`, `/spots` (GET/POST), `/spots/<id>` (PATCH/DELETE), `/pilots` (GET, POST nur Admins), `/pilots/<id>` (PATCH/DELETE, nur Admins), `/flights/pilot` (POST `{"flight_ids": [...], "pilot_id": <id> | null | "auto"}`), `/flights/<id>/note` (PUT `{"note": "..."}`, leer löscht sie), `/upload` (POST, multipart-Feld `file`, nur Admins), `/media/<id>/thumb`, `/media/<id>/play`, `/media/<id>/original`.

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

**Aufnahmen:** Integration ein zweites Mal hinzufügen, dann auswählen:
- *Ordner oder Netzwerkspeicher (SMB, NFS)*: Pfad angeben, z. B. `/media/nas/drohne`. Details in [`docs/local-media.md`](docs/local-media.md).
- *OneDrive*: Anmeldung, braucht eine kostenlose Azure-App-Registrierung. Details in [`docs/onedrive.md`](docs/onedrive.md).

**DJI API-Key** (kostenlos): auf [developer.dji.com](https://developer.dji.com) registrieren → *Developer Center* → *Create App* → Typ **Open API** → E-Mail bestätigen → *App Key* kopieren.

## Eigenes Dashboard in der Seitenleiste

Die Integration registriert beim Start ein vollwertiges Panel **„Drohnenflüge"** in der HA-Seitenleiste – kein Lovelace-Dashboard, sondern eine eigene Seite mit drei Ansichten. Die zuletzt gewählte merkt sich der Browser.

**Flüge**
- **Vor dem nächsten Flug**: oben eine Liste mit allem, was der letzte Flug jeder Drohne bzw. jedes Akkus gemeldet hat. Das sind Vorfälle (z. B. Smart-RTH), eine volle oder fast volle SD-Karte (weniger als 10 min Video), Kartenfehler (keine Karte, schreibgeschützt, zu langsam, Formatieren empfohlen, …) und Akkus, die über 60 °C warm wurden, unter 3,0 V pro Zelle entladen wurden, deren Zellen mehr als 0,2 V auseinanderlagen oder die unter 80 % Kapazität bzw. Lebensdauer liegen. „Erledigt“ blendet einen Hinweis aus, bis ein neuerer Flug ihn wieder meldet. Dieselbe Liste steht im Sensor „Hinweise vor dem nächsten Flug“ (Anzahl, Attribut `items`), z. B. für eine Benachrichtigung
- Statistik-Kacheln (Flüge, Flugzeit, Strecke, max. Höhe/Speed, letzter Flug) über den gefilterten Zeitraum
- Filter: Zeitraum (7 Tage … alles), Drohne (ab zwei Drohnen), Pilot (sobald es Piloten gibt: alle, ein Pilot oder „Ohne Pilot“), Heatmap an/aus, DIPUL-Zonen an/aus. Ist der angemeldete HA-Benutzer mit einem Piloten verknüpft, startet das Panel mit dessen Flügen
- **Piloten** (Symbol neben den Filtern, nur Admins): Piloten anlegen, umbenennen und löschen, je Pilot den HA-Benutzer und die Drohnen wählen, deren Flüge ihm automatisch gehören. Eine Drohne und ein Benutzer gehören immer nur zu einem Piloten. Löschen entfernt nur die Zuordnung, nicht die Flüge
- Große Karte, die die volle Höhe nutzt
- Flugliste rechts (auf dem Handy darunter), nach Tagen gruppiert; Klick auf einen Flug zoomt auf ihn und hebt ihn hervor, nochmal klicken hebt die Auswahl auf. Das Diagramm-Symbol am Flug oder „Details“ im Popup öffnet die Ansicht *Flug*

**Flug**
- Kennzahlen: Dauer, Strecke, maximale Entfernung vom Home-Punkt, max. Höhe und Speed, Akku, Videolänge; Hinweis bei Vorfall, voller SD-Karte oder fehlendem API-Key
- Track auf der Karte und Verlaufsdiagramme über die Flugzeit: Höhe über dem Start, Geschwindigkeit, Entfernung vom Home-Punkt, Akku und Akku-Temperatur, darüber ein Band mit den Flugmodi (Normal, Sport, ActiveTrack, RTH, …). Eingriffe des Flugcontrollers (RTH, Landung, …) sind als gestrichelte Linien eingezeichnet. Mit Maus oder Finger über die Diagramme fahren zeigt die Werte an dieser Stelle und die Position auf der Karte
- Mit Aufnahmen: ein Band „Medien“ im Verlauf zeigt, wann gefilmt bzw. fotografiert wurde. Läuft ein Video, wandern Diagramm-Cursor und Kartenposition mit; ein Klick in den Verlauf springt im Video an diese Stelle (bzw. wählt die passende Aufnahme). In der Großansicht stehen die aktuellen Werte und eine kleine Höhenkurve unter dem Video. Videos werden am Aufnahmestart ausgerichtet, den das Flugprotokoll festhält (der Zeitstempel im Dateinamen liegt rund 2 s zu früh); daher kommt auch die Länge, wenn die Quelle keine kennt. Bleibt ein Rest, lässt sich jede Aufnahme sekundenweise verschieben (Versatz − / +, wird im Browser gespeichert)
- Flugmodi mit Zeitanteilen, Ereignisliste, Akku (Seriennummer, Zyklen, Kapazität, Temperatur, Zellspannungen), Aufnahme (Video, SD-Karte), Technik (Seriennummer, App- und Log-Version, Datei) und Export als GPX/KML/GeoJSON
- Auf breiten Bildschirmen teilen sich Video, Karte und Verlauf den restlichen Bildschirm (links Video über Karte, rechts der Verlauf). Die Trenner dazwischen und der Griff darunter lassen sich ziehen (auch per Pfeiltasten), ein Doppelklick setzt sie zurück; die Aufteilung merkt sich der Browser. Auf dem Handy steht alles untereinander
- ‹ und › blättern zum vorherigen bzw. nächsten Flug
- Notiz: „+ Notiz“ unter den Kennzahlen öffnet ein Textfeld (bis 2000 Zeichen), das beim Tippen nach einer kurzen Pause und beim Verlassen des Felds speichert. Die erste Zeile erscheint in der Flugliste. Ein erneutes Einlesen des Logs lässt die Notiz stehen
- Pilot: „über die Drohne“ (Standard), ein bestimmter Pilot oder „Kein Pilot“; die Wahl gilt sofort und überstimmt die Drohne. Das dürfen alle Benutzer, nicht nur Admins
- Die Diagramme brauchen entschlüsselte Logs (API-Key). Flüge, die vor dieser Version importiert wurden, bekommen sie beim nächsten Scan

**Planen**
- Karte mit den DIPUL-Zonen; ein Tipp auf die Karte öffnet „Neuer Ort“ (siehe unten). „Flüge einblenden“ zeigt die bisherigen Tracks
- **Suche** über der Karte: Postleitzahl, Ort, Adresse oder Koordinaten eingeben, Enter. Die Karte springt hin und setzt einen Marker, dessen Popup „Ort merken“ (Name schon vorbelegt) und Navigation anbietet. Koordinaten gehen in allen üblichen Schreibweisen: `48.13743, 11.57549`, `48,13743 11,57549`, `N 48.13743 E 11.57549`, `48°08'14.7"N 11°34'31.8"E` (so kopiert man sie aus Google Maps) oder ein Google-Maps-Link mit `@48.13743,11.57549`. Koordinaten werden lokal erkannt; alles andere fragt der Browser bei [Nominatim](https://nominatim.org) (OpenStreetMap) an, eine reine PLZ zuerst als Postleitzahl im Land der HA-Instanz
- **Mein Standort**: der Knopf unter dem Zoom zeigt die eigene Position (blauer Punkt mit Genauigkeitskreis) und zoomt hin; im Popup „Hier merken“. Im Browser braucht das die Standortfreigabe und HTTPS (sonst geben Browser den Standort nicht heraus). Klappt das nicht – etwa in der Android-App, deren WebView keinen Standort an Webseiten gibt, oder über `http://` –, nimmt die Karte den letzten Standort, den die Companion App an Home Assistant meldet (Person des angemeldeten Benutzers bzw. ihr GPS-Tracker; das Popup zeigt Gerät und Alter). Dafür muss in der App die Standortverfolgung an und das Gerät der eigenen Person zugeordnet sein
- Liste der gemerkten Orte mit Navigations-Link und Löschen; Klick zoomt auf den Ort

**Überall**
- ↻-Button oben rechts scannt den Log-Ordner sofort
- ⇧-Button oben rechts: **Flugaufzeichnungen hochladen** (nur für Admins). Alternativ Dateien oder den ganzen Ordner `FlightRecord` auf die Seite ziehen. Die Dateien landen im Log-Ordner und werden sofort importiert; schon vorhandene werden erkannt und nicht doppelt gespeichert
- Hinweisleiste, wenn Flüge ohne GPS-Track importiert wurden (fehlender API-Key) oder nicht unterstützte Dateien im Ordner liegen

Abschaltbar über *Integration → Konfigurieren → „In der Seitenleiste anzeigen"*. Die Position in der Seitenleiste lässt sich wie bei jedem Panel per Rechtsklick bzw. über *Profil → Seitenleiste bearbeiten* ändern.

## Orte merken (DIPUL-Zonen)

Im Panel unter *Planen* auf eine freie Stelle der Karte tippen: Es öffnet sich „Neuer Ort“ mit den DIPUL-Zonen an diesem Punkt. Die Karte blendet dort die Geozonen der [DIPUL](https://www.dipul.de) (DFS, Digitale Plattform Unbemannte Luftfahrt) ein, ab Zoomstufe 8. Ein Tipp auf die Karte fragt die Zonen an diesem Punkt ab (z. B. „Kontrollzone Frankfurt Main (EDDF) Zone 4 (691 ft MSL – 2500 ft MSL)“, „Vogelschutzgebiet Hessische Rhön“). Name und Notiz eingeben, **Merken**.

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
pilot: me          # optional: me (der mit dem HA-Benutzer verknüpfte Pilot), Name, ID oder none (ohne Pilot)
heatmap: true
markers: true      # Startpunkte
home: true         # Home-Punkte
tiles: ha          # Start-Ebene: ha (HA-eigener OSM-Proxy, Default) | carto | satellite (Esri) | topo
tile_switch: true  # Umschalter „Karte | Satellit“ oben rechts; die Wahl merkt sich der Browser
spot_on_click: false # Klick auf die Karte öffnet „Neuer Ort“ (im Panel immer an)
locate: false      # Knopf „Mein Standort“ unter dem Zoom (Browser über HTTPS, sonst Standort der eigenen Person; im Panel unter Planen an)
height: 450
```

Satellitenbilder kommen von Esri World Imagery (mit Orts- und Grenznamen darüber) und werden im Dark Mode nicht invertiert.

Weitere Optionen: `dipul` (DIPUL-Geozonen einblenden, Default false), `spots` (gemerkte Orte anzeigen, Default true bei `mode: all`), `flights` (false: keine Tracks, nur Orte und Zonen), `scan_button` (↻ im Titel, Default true), `limit`, `since` (ISO-Datum), `line_color`, `line_weight`, `max_points` (Punkte pro Track in der Übersicht, Default 400), `dark` (`auto`/`true`/`false`), `refresh_entity` (Default `sensor.dji_flight_log_last_import`), `refresh_seconds`.

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
- Akku-Warnung, wenn ein Akku über 60 °C warm wurde, die Zellen mehr als 0,2 V auseinanderlagen oder eine Zelle unter 3,0 V fiel

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
