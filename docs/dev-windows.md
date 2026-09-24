# Entwicklung unter Windows

Home Assistant Core unterstützt Windows offiziell nicht; für Tests reicht aber ein kleiner Workaround.

```powershell
# kurzer Pfad, sonst scheitert pip an der Windows-Pfadlänge
python -m venv C:\tmp\djiha\venv
C:\tmp\djiha\venv\Scripts\pip install homeassistant pydjirecord pytest-homeassistant-custom-component ruff
```

Zwei Unix-only Module als Stubs anlegen (`C:\tmp\djiha\venv\Lib\site-packages\`):

`fcntl.py`
```python
LOCK_EX = LOCK_NB = LOCK_SH = LOCK_UN = 0
def flock(fd, op): pass
def fcntl(fd, op, arg=0): return 0
def ioctl(fd, req, arg=0, mutate_flag=True): return 0
```

`resource.py`
```python
RLIMIT_NOFILE = 7
RLIM_INFINITY = -1
def getrlimit(res): return (1024, 4096)
def setrlimit(res, limits): pass
```

Der Socket-Block des HA-Test-Harness wird in `tests/conftest.py` unter Windows neutralisiert (der Windows-Eventloop braucht ein AF_INET-Socketpair).

```powershell
C:\tmp\djiha\venv\Scripts\python -m pytest -q
C:\tmp\djiha\venv\Scripts\ruff check custom_components tests
C:\tmp\djiha\venv\Scripts\ruff format custom_components tests
```

## Echte Logs testen

Eigene Logs nach `flightrecords/` legen (gitignored). Einmal mit API-Key entschlüsseln:

```powershell
$env:DJI_API_KEY = "<key>"; C:\tmp\djiha\venv\Scripts\python scripts\decode_flightrecords.py
```

Das schreibt pro Log nach `flightrecords/decoded/`:
- `*.frames.json`: alle Frames plus die Kennzahlen, die pydjirecord daraus berechnet
- `*.records.json`: die Rohdatensätze (Firmware, SD-Karte, Parameter …)
- `*.keychains.json`: die Schlüssel, die DJI für das Log zurückgegeben hat

Mit den gespeicherten Keychains laufen `tests/test_real_logs.py` und weitere Decode-Läufe ohne Key und ohne Netz. Die Dateien enthalten Seriennummern und GPS-Positionen.

```powershell
C:\tmp\djiha\venv\Scripts\python -m pytest tests/test_real_logs.py -s
```
