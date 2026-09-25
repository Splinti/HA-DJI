# Aufnahmen aus einem Ordner oder vom NAS

Statt aus OneDrive kann die Integration Videos und Fotos auch aus einem Ordner lesen, auf den Home Assistant Zugriff hat. Dazu gehören Freigaben auf einem NAS: Home Assistant bindet **SMB/CIFS** und **NFS** selbst ein, die Integration liest danach nur noch Dateien. Zugangsdaten für das NAS braucht sie nicht.

```
Drohne / SD-Karte ──PC-Skript──▶ \\nas\drohne\medien  (SMB-Freigabe)
                                          │
              Home Assistant: Netzwerkspeicher „Medien“ → /media/nas
                                          │
     dji_flightlog: Ordner einlesen, MP4-Dauer, Vorschaubilder (ffmpeg)
                                          │
     Zeitabgleich Flug ↔ Aufnahme → Panel (Vorschau, Player, Download) · Karten-Popup
```

Die Zuordnung zu den Flügen und die erkannten Dateien sind dieselben wie bei OneDrive, siehe [`onedrive.md`](onedrive.md#welche-dateien) („Welche Dateien?“ und „Zuordnung“).

## Einrichten

### 1. Freigabe einbinden (nur bei NAS)

*Einstellungen → System → Speicher → Netzwerkspeicher hinzufügen*

| Feld | Wert |
|---|---|
| Name | z. B. `nas` (wird zu `/media/nas`) |
| Verwendung | **Medien** |
| Server | IP oder Name des NAS |
| Protokoll | Samba/Windows (CIFS) oder NFS |
| Freigabe | z. B. `drohne` |
| Benutzer/Passwort | bei CIFS; ein Benutzer mit **Lesezugriff** reicht |

Home Assistant hängt die Freigabe dann unter `/media/<Name>` ein. Bei Docker- oder Core-Installationen bindet man sie stattdessen auf dem Host ein und reicht sie in den Container durch.

Ohne NAS geht jeder lokale Ordner, z. B. `/media/drohne` (per Samba-Add-on unter `\\homeassistant\media\drohne` erreichbar) oder `/share/dji/medien`.

### 2. Ordner in der Integration hinzufügen

*Einstellungen → Geräte & Dienste → DJI Flight Log → Eintrag hinzufügen → „Ordner oder Netzwerkspeicher“*

| Option | Default | Bedeutung |
|---|---|---|
| Ordner | `/media` | Absoluter Pfad; Unterordner werden mitgelesen |
| Scan-Intervall | 900 s | Wie oft der Ordner neu eingelesen wird. Der Button „Ordner einlesen“ und ↻ im Panel lösen es sofort aus |
| Toleranz um einen Flug | 120 s | Wie bei OneDrive |

Man kann mehrere Ordner (und OneDrive-Konten) gleichzeitig verbinden.

## Was die Integration dabei tut

- **Einlesen:** Bei jedem Durchlauf wird der Ordner komplett aufgelistet. Dateien mit unveränderter Größe und Änderungszeit werden nicht erneut geöffnet. Ordner, die mit `.`, `@`, `#` oder `$` beginnen (Synology `@eaDir`, Papierkörbe), werden übersprungen.
- **Dauer:** Wird aus dem MP4-Header gelesen (`moov/mvhd`, auch bei `.LRF` und `.OSV`). Dafür werden nur wenige Kilobyte gelesen, auch wenn der Header am Dateiende liegt.
- **Vorschaubilder:** Kommen von ffmpeg (bei HAOS vorhanden), und zwar aus `…_cover.jpg`, sonst aus dem Proxy, sonst aus dem Original. Sie sind 480 px breit und werden in `.storage/dji_flightlog/thumbs` zwischengespeichert.
- **Wiedergabe:** Home Assistant liefert den Proxy (bzw. das normale Video/Foto) selbst aus, mit Range-Anfragen, sodass man im Player spulen kann. Wie bei OneDrive muss der Browser HEVC abspielen können.
- **Original:** Statt „In OneDrive öffnen“ gibt es den Link „Original herunterladen“.
- **Freigabe nicht eingebunden:** Ist der Ordner leer, obwohl vorher Dateien darin lagen, schlägt der Abgleich fehl, statt alle Aufnahmen zu entfernen. Das Panel zeigt dann einen Hinweis.

## Sync-Skript

`-MediaTarget` nimmt jeden Pfad, also auch die NAS-Freigabe direkt:

```powershell
.\scripts\Sync-DjiFlightRecords.ps1 -MediaTarget '\\nas\drohne\medien' -MediaSource 'E:\DJI Avata 360'
```

Oder über das Samba-Add-on direkt in den Medienordner von Home Assistant: `-MediaTarget '\\homeassistant\media\drohne'`.

## Andere Speicherorte

WebDAV (Nextcloud, ownCloud, Synology/QNAP) ist geplant: [#6](https://github.com/Splinti/HA-DJI/issues/6).
