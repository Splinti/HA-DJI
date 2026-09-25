# Flugaufzeichnungen zum HA-Host bringen

Die Integration überwacht nur einen Ordner. Dieses Dokument beschreibt, wie die Logs vom RC 2 bzw. Handy dorthin kommen.

## Wo liegen die Logs?

| Gerät | Pfad | Zugriff |
|---|---|---|
| **DJI RC 2** | `Interner gemeinsamer Speicher\Android\data\dji.go.v5\files\FlightRecord` | USB-C am PC (MTP). Der RC 2 erscheint als „DJI RC 2" im Explorer. |
| **Android-Handy** (DJI Fly, z. B. mit Goggles N3) | `Android\data\dji.go.v5\files\FlightRecord` (Android 11+) bzw. `DJI\dji.go.v5\FlightRecord` (älter) | USB am PC (MTP). |
| iPhone | Dateien-App → *Auf meinem iPhone/DJI Fly/FlightRecords* | Kurzbefehle-Automation, siehe unten |

> **Nicht verwechseln:** Der Export aus **DJI Assistant 2** (Drohne per USB an den PC, „Geräteprotokolle exportieren") liefert eine Datei wie `DJI_Avata_360_2026-09-22_12-34-44.DAT` mit zig MB. Das ist ein Werkstatt-Bundle aus AES-verschlüsselten Einzellogs (`hms/*.log.enc`, `flyctrl_smp/FC_SMP-*.DAT.enc`) – gedacht für den DJI-Support, und nur DJI besitzt die Schlüssel. Es gibt dafür kein öffentliches Tool. Die Integration erkennt solche Dateien und meldet sie als „nicht unterstützt".

Dateien heißen `DJIFlightRecord_YYYY-MM-DD_[HH-MM-SS].txt`. Bei aktuellen Drohnen sind sie verschlüsselt (Log-Version 13+); die Integration entschlüsselt sie mit dem DJI-API-Key.

## Warum kein Sync-APK?

- **RC 2**: DJI blockt die Installation fremder APKs (auch per ADB). Es gibt keinen offiziellen Weg, Software auf den RC 2 zu bringen.
- **Android-Handy**: Seit Android 11 dürfen fremde Apps nicht in `Android/data/<andere App>` lesen (Scoped Storage). Syncthing, FolderSync & Co. sehen den Ordner nicht; ab Android 13 auch nicht mehr über den Dateidialog (SAF). Einzige App-Wege sind Root oder [Shizuku](https://shizuku.rikka.app/) (ADB-Rechte für Apps; nach jedem Neustart neu zu aktivieren).
- **DJI-Cloud-Sync** in DJI Fly lädt nur zu DJI hoch – es gibt keine API, um die Logs wieder herunterzuladen.

Über USB am PC ist der Ordner dagegen bei beiden Geräten ohne Tricks lesbar (MTP läuft mit App-Rechten von DJI Fly bzw. dem System). Daher:

## Am einfachsten: Upload im Panel

Ohne Samba und ohne Skript, von jedem PC mit Browser aus:

1. RC 2 bzw. Handy per USB anstecken (ggf. „Dateiübertragung" bestätigen).
2. Im Explorer den Ordner `FlightRecord` öffnen (Pfad siehe Tabelle oben).
3. Alle Dateien markieren und auf das Panel **„Drohnenflüge"** in Home Assistant ziehen, oder dort den ⇧-Button nutzen.

Die Dateien werden im Log-Ordner gespeichert und sofort importiert; eine Leiste zeigt, wie viele Flüge neu waren, welche schon vorhanden waren und warum eine Datei ggf. nicht importiert wurde. Es reicht also, immer den ganzen Ordner zu ziehen. Hochladen dürfen nur HA-Administratoren.

> Direkt vom RC 2 (MTP-Gerät) klappt es am ehesten per **Drag & Drop** aus dem Explorer. Der Datei-Dialog des Browsers verweigert MTP-Orte je nach Browser; dann den Ordner erst auf den PC kopieren und von dort hochladen.

## Automatisch: PC-Sync-Skript (Windows)

`scripts/Sync-DjiFlightRecords.ps1` durchsucht alle per USB angeschlossenen MTP-Geräte nach dem FlightRecord-Ordner und kopiert neue `.txt`-Dateien auf den HA-Share.

### Einmalig einrichten

1. **Samba-Add-on** in HAOS installieren (Add-on Store → *Samba share*), Benutzer/Passwort setzen, starten.
   Unter Windows ggf. Anmeldedaten hinterlegen: `cmdkey /add:homeassistant /user:<samba-user> /pass:<pw>` (oder `homeassistant.local` / IP).
2. Ordner anlegen: `\\homeassistant\share\dji\flightrecords` (entspricht `/share/dji/flightrecords` in HA).
3. Skript testen (Gerät anstecken, ggf. am RC 2 / Handy „Dateiübertragung" bestätigen):
   ```powershell
   .\scripts\Sync-DjiFlightRecords.ps1 -Target '\\homeassistant\share\dji\flightrecords' -WhatIf
   .\scripts\Sync-DjiFlightRecords.ps1 -Target '\\homeassistant\share\dji\flightrecords'
   ```
4. Als geplante Aufgabe alle 10 Minuten registrieren (läuft unsichtbar; ohne angestecktes Gerät beendet es sich sofort):
   ```powershell
   .\scripts\Sync-DjiFlightRecords.ps1 -Register -IntervalMinutes 10 -Target '\\homeassistant\share\dji\flightrecords'
   ```
   Entfernen: `-Unregister`. Log: `%LOCALAPPDATA%\DjiFlightSync\sync.log`.

Ab dann: RC 2 nach dem Fliegen zum Laden an den PC hängen → Logs landen automatisch in HA, die Integration importiert sie beim nächsten Scan (Standard 5 min), das Event `dji_flightlog_flight_imported` feuert.

### Hinweise
- Der RC 2 muss **eingeschaltet** sein, damit MTP aktiv ist.
- Falls der Explorer den RC 2 zeigt, aber keine Ordner: USB-Modus am Gerät auf „Dateiübertragung" stellen.
- Das Skript vergleicht Name + Größe, kopiert also nichts doppelt. Die Integration dedupliziert zusätzlich per Datei-Hash.

## Aufnahmen (Videos/Fotos) nach OneDrive oder aufs NAS

Dasselbe Skript kopiert mit `-MediaTarget` auch die Aufnahmen (von Drohne/Goggles per USB, SD-Kartenleser oder `-MediaSource`-Ordnern) in den lokalen OneDrive-Ordner oder eine Netzwerkfreigabe, erzeugt Vorschau-Proxys (mit ffmpeg verlustfrei mit Faststart umgepackt) und Titelbilder und legt sie nach Datum ab. Mit `-Stitch360` entsteht für 360°-Aufnahmen zusätzlich ein `…_360.mp4` (equirektangular, H.264), das der Player bevorzugt. Die Integration verknüpft die Aufnahmen dann mit den Flügen – siehe [`onedrive.md`](onedrive.md) bzw. [`local-media.md`](local-media.md); dort stehen auch [alle Schalter](onedrive.md#3-pc-skript).

```powershell
.\scripts\Sync-DjiFlightRecords.ps1 -MediaTarget "$env:OneDrive\Drohne\Medien" -MediaSource 'E:\DJI Avata 360'
.\scripts\Sync-DjiFlightRecords.ps1 -MediaTarget '\\nas\drohne\medien' -MediaSource 'E:\DJI Avata 360' -Stitch360
```

## Alternativen

### Shizuku + Tasker (Android-Handy, ohne PC)
Für Fortgeschrittene: Shizuku per Wireless-Debugging starten, dann kann z. B. Tasker (mit Shizuku-Plugin) `cp`-Befehle mit ADB-Rechten ausführen und den Ordner in einen öffentlichen Ordner spiegeln, den Syncthing-Fork dann zum HA-Host synct. Funktioniert, muss aber nach jedem Neustart des Handys neu aktiviert werden – daher hier nicht als Standardweg.

### ADB over Wi-Fi (Handy)
Wireless-Debugging aktivieren, vom PC aus `adb pull /sdcard/Android/data/dji.go.v5/files/FlightRecord <ziel>`. Funktioniert ohne Root, das Pairing verfällt aber regelmäßig.

### iPhone (falls mal relevant)
Kurzbefehle-App → Automation „Wenn mit WLAN <Heimnetz> verbunden" → *Ordnerinhalt abrufen* (DJI Fly/FlightRecords) → *Datei sichern* auf einen SMB-Speicherort (in der Dateien-App vorher `smb://homeassistant/share` verbinden). iOS erlaubt hier den Zugriff, weil DJI Fly seinen Ordner in der Dateien-App freigibt.

## Manuell per Samba
Ordner per Explorer vom Gerät nach `\\homeassistant\share\dji\flightrecords` ziehen – die Integration kümmert sich um den Rest.
