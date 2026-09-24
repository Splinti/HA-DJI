"""Constants for the DJI Flight Log integration."""

from __future__ import annotations

DOMAIN = "dji_flightlog"

CONF_LOG_DIR = "log_dir"
CONF_API_KEY = "api_key"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MAX_TRACK_POINTS = "max_track_points"
CONF_GEO_LOCATION_LIMIT = "geo_location_limit"
CONF_SIDEBAR_PANEL = "sidebar_panel"

DEFAULT_LOG_DIR = "/share/dji/flightrecords"
DEFAULT_SCAN_INTERVAL = 300  # seconds
DEFAULT_MAX_TRACK_POINTS = 1500
# 0 = off. Any geo_location entity makes HA's auto Overview dashboard add a
# map card, so this is opt-in.
DEFAULT_GEO_LOCATION_LIMIT = 0
DEFAULT_SIDEBAR_PANEL = True

STORAGE_VERSION = 1
# Bump when parsing changes what a flight's summary or track contains: files
# imported by an older parser are re-parsed on the next scan.
# 2: duration/track offsets per log (not since power-on), video time from frames,
#    exports use height above takeoff.
# 3: smart battery serial, cycles, capacity, temperature and cell voltages.
# 4: incident level and actions, SD card capacity.
# 5: per-second profile, flight modes and events for the detail view; max distance.
PARSER_VERSION = 5
STORAGE_KEY = f"{DOMAIN}.flights"
STORAGE_SUBDIR = DOMAIN  # <config>/.storage/dji_flightlog/tracks/<id>.json

# .dat is scanned too, not because it can be parsed, but so the integration can
# tell the user why such a file yields no flight instead of ignoring it silently.
LOG_FILE_SUFFIXES = (".txt", ".dat")

# A DJI Fly flight record is a few hundred KB; a support log bundle is tens of MB.
# The cap keeps a huge file from being read into memory on a Pi/HA Green.
MAX_LOG_FILE_BYTES = 32 * 1024 * 1024

EVENT_FLIGHT_IMPORTED = f"{DOMAIN}_flight_imported"

SERVICE_SCAN = "scan"
SERVICE_IMPORT_FILE = "import_file"
SERVICE_EXPORT_TRACK = "export_track"

ATTR_PATH = "path"
ATTR_FLIGHT_ID = "flight_id"
ATTR_FORMAT = "format"

EXPORT_FORMATS = ("gpx", "kml", "geojson")

STATIC_URL_BASE = f"/{DOMAIN}_static"
CARD_FILENAME = "dji-flight-map-card.js"
CARD_URL = f"{STATIC_URL_BASE}/{CARD_FILENAME}"

# Sidebar panel (panel_custom): element name, module and the URL it lives at.
PANEL_FILENAME = "dji-flightlog-panel.js"
PANEL_URL = f"{STATIC_URL_BASE}/{PANEL_FILENAME}"
PANEL_ELEMENT = "dji-flightlog-panel"
PANEL_URL_PATH = "dji-flightlog"
PANEL_TITLE = "Drohnenflüge"
PANEL_ICON = "mdi:quadcopter"

# Files whose keychain fetch failed are retried on the next scan; a file
# that fails to parse for any other reason is remembered so it is not
# re-parsed on every scan.
STATUS_OK = "ok"
STATUS_HEADER_ONLY = "header_only"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"

REASON_SUPPORT_BUNDLE = "support_bundle"
REASON_TOO_LARGE = "too_large"
REASON_FC_DAT = "fc_dat"

# Per-file outcome of a browser upload (plus STATUS_FAILED / STATUS_UNSUPPORTED).
UPLOAD_IMPORTED = "imported"
UPLOAD_DUPLICATE = "duplicate"
UPLOAD_RETRY = "retry"  # DJI keychain fetch failed; the next scan tries again
UPLOAD_REJECTED = "rejected"
REASON_NOT_TXT = "not_txt"
