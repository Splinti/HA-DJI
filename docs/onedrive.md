# Aufnahmen aus OneDrive verknüpfen

Die Integration kann Videos und Fotos, die in einem OneDrive-Ordner liegen, automatisch den Flügen zuordnen. Im Panel erscheinen sie dann als Vorschaubilder am Flug, in der Karte im Popup. Abgespielt wird aus OneDrive; über Home Assistant laufen nur Vorschaubilder (klein, gecacht) und eine Weiterleitung.

```
Drohne / SD-Karte ──PC-Skript──▶ lokaler OneDrive-Ordner ──OneDrive-Client──▶ OneDrive
                                                                              │
             dji_flightlog ◀── Microsoft Graph (nur lesen: Dateiliste, Vorschaubilder, Links)
                   │
     Zeitabgleich Flug ↔ Aufnahme → Panel (Vorschau, Player) · Karten-Popup
```

## Welche Dateien?

| Datei | Inhalt | Im Browser? | Ablegen? |
|---|---|---|---|
| `DJI_…_D.OSV` (Avata 360) | 360°-Original: zwei Fisheye-Streams 3840×3840, HEVC 10 Bit, ~180 Mbit/s (≈ 1,3 GB/min), plus eingebettetes Titelbild | Nein, muss erst in DJI Studio / LightCut gestitcht werden | Ja, als Archiv |
| `DJI_…_D.LRF` | Proxy der Kamera; bei der Avata 360: 1920×960 HEVC 8 Bit, **ungestitcht** (beide Linsen nebeneinander), ~15 Mbit/s | Ja, sofern der Browser HEVC kann (Edge/Chrome/Safari mit Hardware-Decoder) | Empfohlen; das Skript benennt ihn in `…_proxy.mp4` um |
| `DJI_…_D.MP4` (Avata 2, Neo) | Normales Video (H.264/H.265, ggf. D-Log M) | H.264 immer, H.265 meist | Ja |
| `DJI_…_D.JPG` / `.DNG` | Foto / Raw-Foto | JPG ja, DNG nein | Ja; DNG wird am Foto als „RAW“ angezeigt |
| `DJI_…_D.SRT` | Telemetrie-Untertitel | – | Optional |

„Rohdateien“ (OSV, DNG, 10-Bit-Log-Video) gehören also ins Archiv. Zum Anschauen im Browser dienen der Proxy bzw. OneDrives eigener Player (Link „OneDrive“).

## Zuordnung

Die Aufnahmezeit steht im Dateinamen (`DJI_20260921190306_…` = 21.09.2026 19:03:06 **Ortszeit** der Drohne). Die Integration rechnet sie mit der Zeitzone von Home Assistant in UTC um und ordnet die Aufnahme dem Flug zu, dessen Zeitraum sie überlappt (± Toleranz, Standard 120 s). Bei mehreren Kandidaten gewinnt der zeitlich nächste Flug.

Andere Namen (z. B. Clips, die DJI Fly in der Handy-Galerie gespeichert hat) werden über einen Zeitstempel `YYYYMMDD_HHMMSS` im Namen oder über das von OneDrive ausgelesene Aufnahmedatum zugeordnet.

Zusammengehörige Dateien (gleicher Name ohne Endung, im selben Ordner) werden zu **einer** Aufnahme zusammengefasst: Original + `_proxy.mp4` + `_cover.jpg` + `.DNG` + `.SRT`.

## Einrichten

### 1. Azure-App registrieren (einmalig, kostenlos)

1. [Microsoft Entra – App-Registrierungen](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) → *Neue Registrierung*
   - Name: z. B. `Home Assistant DJI`
   - Unterstützte Kontotypen: **Persönliche Microsoft-Konten** (bzw. „Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten“ für OneDrive for Business)
   - Umleitungs-URI: Plattform **Web**, `https://my.home-assistant.io/redirect/oauth`
2. *Zertifikate & Geheimnisse* → *Neuer geheimer Clientschlüssel* → Wert kopieren (wird nur einmal angezeigt).
3. *API-Berechtigungen* → Microsoft Graph → Delegiert: `Files.Read`, `offline_access` (meist schon vorausgewählt: `User.Read`, kann bleiben).
4. *Übersicht* → **Anwendungs-ID (Client)** kopieren.

### 2. In Home Assistant

1. *Einstellungen → Geräte & Dienste → Integration hinzufügen → DJI Flight Log*. Da das Flugbuch schon eingerichtet ist, fragt HA nach dem Speicherort: **OneDrive** wählen.
2. Beim ersten Mal fragt HA nach den *Anwendungsanmeldedaten*: Client-ID und geheimen Schlüssel aus Schritt 1 eintragen.
3. Bei Microsoft anmelden und den Lesezugriff bestätigen.
4. Ordner wählen: Die Auswahl zeigt die Unterordner des aktuellen Ordners, ein Klick öffnet einen, „⬆ Eine Ebene höher“ geht zurück, „✓ … verwenden“ übernimmt den aktuellen Ordner. Sie startet in `Drohne/Medien`, falls es den Ordner gibt, sonst im OneDrive-Stamm. Unterordner werden mitgelesen.

Optionen (später unter *Konfigurieren* änderbar):

| Option | Default | Bedeutung |
|---|---|---|
| Ordner | `Drohne/Medien` | Wird rekursiv gelesen; ändern über „Anderen Ordner wählen“ |
| Sync-Intervall | 900 s | Abgleich per Delta-Abfrage, d. h. nach dem ersten Mal werden nur Änderungen geladen |
| Toleranz um einen Flug | 120 s | Aufnahmen, die kurz vor dem Start/nach der Landung beginnen, zählen noch zum Flug |
| Flugaufzeichnungen übernehmen | aus | DJI-Fly-Logs aus dem Ordner in den Log-Ordner kopieren, siehe [unten](#flugaufzeichnungen-übernehmen-z-b-zweiter-pilot) |

Der ↻-Button im Panel bzw. `dji_flightlog.scan` gleicht OneDrive sofort mit ab.

Mehrere OneDrive-Konten sind möglich (Integration erneut hinzufügen, siehe [Mehrere Konten und Speicherorte](#mehrere-konten-und-speicherorte)). Läuft die Anmeldung ab, zeigt HA das an der Integration an und bietet die erneute Anmeldung an.

### 3. PC-Skript

Das Sync-Skript kopiert die Aufnahmen in den lokalen OneDrive-Ordner, den Rest erledigt der OneDrive-Client:

```powershell
# Probelauf
.\scripts\Sync-DjiFlightRecords.ps1 -MediaTarget "$env:OneDrive\Drohne\Medien" -MediaSource 'E:\DJI Avata 360' -WhatIf

# Als geplante Aufgabe (Flugprotokolle + Aufnahmen)
.\scripts\Sync-DjiFlightRecords.ps1 -Register -IntervalMinutes 10 `
    -Target '\\homeassistant\share\dji\flightrecords' -MediaTarget "$env:OneDrive\Drohne\Medien"
```

Quellen: `DCIM` angeschlossener MTP-Geräte (Drohne, Goggles, RC), `DCIM` von Wechseldatenträgern (SD-Kartenleser) und alle Ordner aus `-MediaSource`. Ablage als `<MediaTarget>\<Jahr>\<Jahr-Monat-Tag>\`.

| Schalter | Wirkung |
|---|---|
| `-NoProxy` | `.LRF` nicht kopieren |
| `-NoCover` | kein `_cover.jpg` (sonst: bei `.OSV` das eingebettete Titelbild, bei `.MP4` ein Frame; braucht **ffmpeg**) |
| `-Stitch360` | Proxy von 360°-Aufnahmen per ffmpeg zu einem equirektangularen H.264-Video umrechnen: spielt in jedem Browser, dauert etwa Echtzeit |
| `-FisheyeFov 190` | Bildwinkel der Linsen für `-Stitch360` |
| `-FfmpegPath` | Pfad zu `ffmpeg.exe`, falls nicht im `PATH` |

ffmpeg z. B. per `winget install Gyan.FFmpeg`.

> Speicherplatz: 360°-Originale haben mehrere GB pro Clip. Mit OneDrive „Dateien bei Bedarf“ kann Windows die lokale Kopie nach dem Upload freigeben (Rechtsklick → *Speicherplatz freigeben*).

## Mehrere Konten und Speicherorte

Beliebig viele OneDrive-Konten und Ordner lassen sich gleichzeitig verbinden (Integration jeweils erneut hinzufügen). Das Panel führt alle Aufnahmen zusammen, z. B. die eigenen vom NAS und die eines zweiten Piloten aus dessen OneDrive.

**Dieselbe Aufnahme an zwei Orten** (gleicher Dateiname, gleiche Größe) wird nur einmal angezeigt. Es bleibt die Kopie mit den meisten Begleitdateien (Proxy, Titelbild, RAW, …), bei Gleichstand die des zuerst eingerichteten Speicherorts. In „Aufnahmen ohne Flug“ zählen solche Kopien nicht mit.

### Flugaufzeichnungen übernehmen (z. B. zweiter Pilot)

Aufnahmen werden nur Flügen zugeordnet, die im eigenen Log-Ordner liegen. Damit die Flüge eines zweiten Piloten dort ankommen, ohne dass er Zugriff auf das eigene Heimnetz braucht, kann ein Speicherort auch dessen **Flugaufzeichnungen** liefern:

1. Er legt seine DJI-Fly-Logs mit in den verbundenen Ordner, z. B. mit dem Sync-Skript auf seinem PC:
   ```powershell
   .\scripts\Sync-DjiFlightRecords.ps1 -Register -IntervalMinutes 10 `
       -Target "$env:OneDrive\Drohne\Medien\Flugaufzeichnungen" -MediaTarget "$env:OneDrive\Drohne\Medien"
   ```
2. In den Optionen seines Speicherorts **„Hier gefundene Flugaufzeichnungen … übernehmen“** einschalten.

Bei jedem Abgleich werden neue `FlightRecord_*.txt` bzw. `DJIFlightRecord_*.txt` (irgendwo unterhalb des Ordners) in den eigenen Log-Ordner **kopiert** und importiert, wie beim Upload im Panel:

- **Source of Truth bleibt der Log-Ordner.** Am Speicherort wird nichts geändert oder gelöscht; umgekehrt entfernt das Löschen dort keinen Flug.
- Jede Datei wird einmal kopiert (ändert sich ihre Größe, noch einmal). Liegt dieselbe Datei schon im Log-Ordner, entsteht keine zweite Kopie; eine andere Datei gleichen Namens bekommt `_2`.
- Verschlüsselte Logs werden mit dem eigenen DJI-API-Key entschlüsselt. Schlägt das fehl, holt der nächste Scan des Log-Ordners es nach.
- Der Import läuft im Hintergrund; bei einer langen Flughistorie kann der erste Durchlauf dauern. Der Sensor „Letzter Abgleich“ zeigt im Attribut `flight_records_imported`, wie viele Dateien übernommen wurden.
- Seine Drohnen und Akkus erscheinen als eigene Geräte; die Gesamtstatistik zählt beide Piloten.

## Anzeige

- **Panel**: Flüge mit Aufnahmen tragen ein Kamerasymbol mit Anzahl. Nach Auswahl des Flugs erscheint darunter eine Leiste mit Vorschaubildern:
  Klick spielt den Proxy bzw. das Video im Overlay ab, „OneDrive“ öffnet die Datei in OneDrive. Steht im Flugprotokoll, dass aufgenommen wurde, aber es gibt keine passende Datei, zeigt der Flug einen Hinweis.
- **Karte**: Das Popup eines Flugs zeigt die Vorschaubilder; Klick öffnet OneDrive.
- **API**: `/api/dji_flightlog/flights` liefert je Flug `media: [{id, kind, name, start, duration_s, web_url, thumb, play, download, has_raw, …}]` sowie `media` (Status je Konto bzw. Ordner). `download` gibt es nur bei Quellen ohne `web_url` (lokaler Ordner). `thumb`/`play` sind signierte URLs (6 h gültig), damit `<img>`/`<video>` ohne Auth-Header funktionieren.

## Datenschutz / Rechte

- Nur Lesezugriff (`Files.Read`) auf das eigene OneDrive; die Integration schreibt nichts dorthin (Flugaufzeichnungen werden nur heruntergeladen).
- Gespeichert werden Dateiliste des Ordners (`/config/.storage/dji_flightlog.media.<entry_id>`) und Vorschaubilder (`/config/.storage/dji_flightlog/thumbs/`), beides im HA-Backup enthalten.
