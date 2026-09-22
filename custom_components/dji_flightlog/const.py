"""Constants for the DJI Flight Log integration."""

from __future__ import annotations

DOMAIN = "dji_flightlog"

CONF_LOG_DIR = "log_dir"
CONF_API_KEY = "api_key"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MAX_TRACK_POINTS = "max_track_points"
CONF_GEO_LOCATION_LIMIT = "geo_location_limit"

DEFAULT_LOG_DIR = "/share/dji/flightrecords"
DEFAULT_SCAN_INTERVAL = 300  # seconds
DEFAULT_MAX_TRACK_POINTS = 1500
DEFAULT_GEO_LOCATION_LIMIT = 200

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.flights"
STORAGE_SUBDIR = DOMAIN  # <config>/.storage/dji_flightlog/tracks/<id>.json

LOG_FILE_SUFFIXES = (".txt",)

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

# Files whose keychain fetch failed are retried on the next scan; a file
# that fails to parse for any other reason is remembered so it is not
# re-parsed on every scan.
STATUS_OK = "ok"
STATUS_HEADER_ONLY = "header_only"
STATUS_FAILED = "failed"
