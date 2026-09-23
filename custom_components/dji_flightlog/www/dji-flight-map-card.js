/*
 * dji-flight-map-card
 *
 * Lovelace card that draws the tracks imported by the dji_flightlog
 * integration on a Leaflet map. Leaflet is served by the integration itself
 * (/dji_flightlog_static/…) so the card works without internet access,
 * except for the map tiles.
 *
 * Example:
 *   type: custom:dji-flight-map-card
 *   title: Drohnenflüge
 *   mode: all            # all | last | flight
 *   heatmap: true
 *   days: 365
 *   dipul: true          # overlay the German UAS geo zones (DIPUL)
 *   spots: true          # show saved spots (default in mode: all)
 *
 * The same module also defines custom:dji-spots-card, a list of the saved
 * spots with a Google Maps navigation link each:
 *   type: custom:dji-spots-card
 *   title: Gemerkte Orte
 */

const STATIC = "/dji_flightlog_static";
const API = "dji_flightlog";
const CARD_VERSION = "0.2.2";

const PALETTE = [
  "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
  "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
];

// Default: Home Assistant's own tile proxy (map_tiles, HA >= 2026.8). It
// fetches OpenStreetMap tiles server-side with a proper User-Agent, so the
// browser needs neither a Referer nor a third-party key. The token comes
// from the websocket API and rotates every 30 min (two are valid at a time).
const OSM_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const CARTO_ATTR = OSM_ATTR + ' &copy; <a href="https://carto.com/attributions">CARTO</a>';
const TILES = {
  ha: {
    url: "/api/map_tiles/raster/{z}/{x}/{y}.png?token={token}",
    attribution: OSM_ATTR,
    maxZoom: 20,
    maxNativeZoom: 19,
    token: true,
  },
  carto: {
    url: "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    dark: "https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}{r}.png",
    attribution: CARTO_ATTR,
    maxZoom: 20,
  },
  satellite: {
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Tiles &copy; Esri",
    maxZoom: 19,
  },
  topo: {
    url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    attribution: "&copy; OpenStreetMap, SRTM | &copy; OpenTopoMap",
    maxZoom: 17,
  },
};
TILES.osm = TILES.ha; // backwards compatible alias

// DIPUL (Digitale Plattform Unbemannte Luftfahrt, run by DFS): the official
// German UAS geo zones. The WMS sends CORS headers, so the browser queries it
// directly. Licence CC BY-ND 4.0: tiles are shown unchanged and attributed.
const DIPUL_WMS = "https://uas-betrieb.de/geoservices/dipul/wms";
const DIPUL_ATTR =
  'Geozonen &copy; <a href="https://www.dipul.de" target="_blank" rel="noopener">DFS / dipul</a> (CC BY-ND 4.0)';
// Below this zoom a tile covers hundreds of km and takes seconds to render.
const DIPUL_MIN_ZOOM = 8;
const DIPUL_LAYERS = {
  flughaefen: "Flughafen",
  flugplaetze: "Flugplatz",
  kontrollzonen: "Kontrollzone",
  flugbeschraenkungsgebiete: "Flugbeschränkungsgebiet",
  temporaere_betriebseinschraenkungen: "Temporäre Betriebseinschränkung",
  modellflugplaetze: "Modellflugplatz",
  haengegleiter: "Hängegleitergelände",
  naturschutzgebiete: "Naturschutzgebiet",
  nationalparks: "Nationalpark",
  "ffh-gebiete": "FFH-Gebiet",
  vogelschutzgebiete: "Vogelschutzgebiet",
  wohngrundstuecke: "Wohngrundstück",
  freibaeder: "Freibad / Badestrand",
  industrieanlagen: "Industrieanlage",
  kraftwerke: "Kraftwerk",
  umspannwerke: "Umspannwerk",
  stromleitungen: "Stromleitung",
  windkraftanlagen: "Windkraftanlage",
  bundesautobahnen: "Bundesautobahn",
  bundesstrassen: "Bundesstraße",
  bahnanlagen: "Bahnanlage",
  binnenwasserstrassen: "Binnenwasserstraße",
  seewasserstrassen: "Seewasserstraße",
  schifffahrtsanlagen: "Schifffahrtsanlage",
  krankenhaeuser: "Krankenhaus",
  justizvollzugsanstalten: "Justizvollzugsanstalt",
  militaerische_anlagen: "Militärische Anlage",
  labore: "BSL-4-Labor",
  behoerden: "Behörde / Verfassungsorgan",
  diplomatische_vertretungen: "Diplomatische Vertretung",
  internationale_organisationen: "Internationale Organisation",
  polizei: "Polizei",
  sicherheitsbehoerden: "Sicherheitsbehörde",
};
const DIPUL_QUERY = Object.keys(DIPUL_LAYERS)
  .map((l) => `dipul:${l}`)
  .join(",");

/** DIPUL zones at one point. GetFeatureInfo refuses JSON, so this parses text/plain. */
async function queryDipul(lat, lon) {
  const d = 0.0003; // ~30 m; the point is the centre pixel of a 101 px image
  const q = new URLSearchParams({
    service: "WMS",
    version: "1.3.0",
    request: "GetFeatureInfo",
    layers: DIPUL_QUERY,
    query_layers: DIPUL_QUERY,
    styles: "",
    crs: "EPSG:4326",
    // WMS 1.3.0 with EPSG:4326 uses lat,lon axis order.
    bbox: [lat - d, lon - d, lat + d, lon + d].join(","),
    width: "101",
    height: "101",
    i: "50",
    j: "50",
    info_format: "text/plain",
    feature_count: "50",
  });
  const res = await fetch(`${DIPUL_WMS}?${q}`);
  const text = await res.text();
  if (!res.ok || text.includes("ServiceException")) throw new Error(`DIPUL-Abfrage fehlgeschlagen (${res.status})`);
  return parseDipul(text);
}

function parseDipul(text) {
  // Blocks look like:
  //   Results for FeatureType 'de.dfs.dipul:kontrollzonen':
  //   --------------------------------------------
  //   name = Frankfurt Main (EDDF) Zone 4
  //   lower_limit_altitude = 690.9
  //   --------------------------------------------
  const zones = [];
  const seen = new Set();
  let layer = null;
  let cur = null;
  const flush = () => {
    if (!cur || !Object.keys(cur).length) return;
    const zone = {
      layer,
      name: cur.generated_name_DE || cur.name || DIPUL_LAYERS[layer] || layer,
      type: cur.type_code || null,
      lower: fmtLimit(cur.lower_limit_altitude, cur.lower_limit_unit, cur.lower_limit_alt_ref),
      upper: fmtLimit(cur.upper_limit_altitude, cur.upper_limit_unit, cur.upper_limit_alt_ref),
      legal: cur.legal_ref || null,
    };
    const key = `${zone.layer}|${zone.name}`;
    if (!seen.has(key)) {
      seen.add(key);
      zones.push(zone);
    }
    cur = null;
  };
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    const head = line.match(/^Results for FeatureType '(?:.*:)?([^':]+)'/);
    if (head) {
      flush();
      layer = head[1];
    } else if (/^-{3,}$/.test(line)) {
      flush();
      cur = {};
    } else if (cur) {
      const kv = line.match(/^([\w-]+)\s*=\s*(.*)$/);
      if (kv) cur[kv[1]] = kv[2];
    }
  }
  flush();
  return zones;
}

function fmtLimit(alt, unit, ref) {
  if (alt == null || alt === "" || Number.isNaN(Number(alt))) return null;
  return [String(Math.round(Number(alt))), unit, ref].filter(Boolean).join(" ");
}

const zoneLabel = (z) => DIPUL_LAYERS[z.layer] || z.type || z.layer || "Zone";

function zoneLimits(z) {
  if (z.upper) return ` (${z.lower || "?"} – ${z.upper})`;
  if (z.lower && !/^0 /.test(z.lower)) return ` (ab ${z.lower})`;
  return "";
}

function zonesHtml(zones, checked) {
  if (zones == null) return `<div class="zones muted">DIPUL-Zonen nicht geprüft.</div>`;
  const when = checked ? ` (Stand ${esc(fmtDate(checked))})` : "";
  if (!zones.length) return `<div class="zones ok">Keine DIPUL-Zone an diesem Punkt${when}.</div>`;
  return `<div class="zones"><div>DIPUL-Zonen${when}:</div><ul>${zones
    .map((z) => `<li><b>${esc(zoneLabel(z))}</b>: ${esc(z.name)}${esc(zoneLimits(z))}</li>`)
    .join("")}</ul></div>`;
}

const mapsUrl = (lat, lon) =>
  `https://www.google.com/maps/dir/?api=1&destination=${lat.toFixed(6)},${lon.toFixed(6)}`;

const PIN_PATH = "M12,2C8.13,2 5,5.13 5,9C5,14.25 12,22 12,22C12,22 19,14.25 19,9C19,5.13 15.87,2 12,2Z";
const SPOT_COLOR = "#ff9800";
const PLAN_COLOR = "#03a9f4";
function spotIcon(L, color = SPOT_COLOR) {
  return L.divIcon({
    className: "dji-spot",
    html: `<svg viewBox="0 0 24 24" width="32" height="32"><path d="${PIN_PATH}" fill="${color}" stroke="#fff" stroke-width="1.3"/><circle cx="12" cy="9" r="2.6" fill="#fff"/></svg>`,
    iconSize: [32, 32],
    iconAnchor: [16, 31],
    popupAnchor: [0, -28],
  });
}

// One token shared by all cards on the page.
let tileToken = null;
let tileTokenTimer = null;
async function fetchTileToken(hass) {
  try {
    const res = await hass.callWS({ type: "map_tiles/access_token" });
    tileToken = res.token || null;
  } catch (err) {
    console.warn("dji-flight-map-card: map_tiles token unavailable (HA < 2026.8?)", err);
    tileToken = null;
  }
  return tileToken;
}
function ensureTokenRefresh(hass, onChange) {
  if (tileTokenTimer) return;
  tileTokenTimer = setInterval(async () => {
    const old = tileToken;
    await fetchTileToken(hass);
    if (tileToken !== old) onChange();
  }, 20 * 60 * 1000);
}
const tokenListeners = new Set();

let leafletPromise = null;
function loadLeaflet() {
  if (window.L && window.L.heatLayer) return Promise.resolve(window.L);
  if (leafletPromise) return leafletPromise;
  const load = (src) =>
    new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = () => reject(new Error(`failed to load ${src}`));
      document.head.appendChild(s);
    });
  leafletPromise = (window.L ? Promise.resolve() : load(`${STATIC}/leaflet.js`))
    .then(() => (window.L.heatLayer ? null : load(`${STATIC}/leaflet-heat.js`)))
    .then(() => window.L);
  return leafletPromise;
}

const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};
const fmtDur = (s) => {
  s = Math.round(s || 0);
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")} min`;
};
const fmtDist = (m) => (m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m || 0)} m`);
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

class DjiFlightMapCard extends HTMLElement {
  static getStubConfig() {
    return { mode: "all", heatmap: false, days: 365 };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._map = null;
    this._layers = null;
    this._flightLayers = {};
    this._focused = null;
    this._tileLayer = null;
    this._tileUrl = null;
    this._lastRefreshKey = null;
    this._loading = false;
    this._pending = false;
    this._mapInit = null;
    this._timer = null;
    this._spots = [];
    this._spotsKey = null;
    this._spotMarkers = {};
    this._pendingSpot = null;
    this._planning = false;
    this._planMarker = null;
    this._planSeq = 0;
    this._hasFlights = false;
    // Callers set `hass`/`config` before this module is loaded (the panel
    // creates the card via innerHTML and imports it afterwards). Such an
    // assignment lands as an *own* property on the element and shadows the
    // prototype setter for good, so the card would never see a hass and stay
    // empty until a page change builds a fresh element. The upgrade runs this
    // constructor, so collect those properties here and re-assign them.
    for (const prop of ["hass", "config"]) {
      if (!Object.prototype.hasOwnProperty.call(this, prop)) continue;
      const value = this[prop];
      delete this[prop];
      this[prop] = value;
    }
  }

  setConfig(config) {
    if (config.mode === "flight" && !config.flight_id) {
      throw new Error("mode: flight requires flight_id");
    }
    this._config = {
      title: "",
      mode: "all",
      height: 400,
      tiles: "ha",
      heatmap: false,
      markers: true,
      home: true,
      fit: true,
      line_weight: 3,
      line_color: null,
      max_points: 400,
      days: null,
      since: null,
      aircraft: null,
      limit: null,
      dark: "auto",
      refresh_entity: "sensor.dji_flight_log_last_import",
      refresh_seconds: 300,
      scan_button: true,
      dipul: false,
      // Saved spots only make sense on the overview map.
      spots: (config.mode || "all") === "all",
      spots_entity: "sensor.dji_flight_log_saved_spots",
      ...config,
    };
    this._render();
    // The map was rebuilt: load everything again for it.
    this._lastRefreshKey = null;
    this._spotsKey = null;
    if (this._hass) this.hass = this._hass;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return; // setConfig() loads once it has a config
    const ent = hass.states[this._config.refresh_entity];
    const key = ent ? ent.state : "none";
    if (key !== this._lastRefreshKey) {
      this._lastRefreshKey = key;
      this._refresh();
    }
    // The saved-spots sensor changes whenever a spot is added, edited or removed.
    const spotsEnt = hass.states[this._config.spots_entity];
    const spotsKey = spotsEnt ? spotsEnt.last_updated : "none";
    if (this._config.spots && spotsKey !== this._spotsKey) {
      this._spotsKey = spotsKey;
      this.reloadSpots();
    }
  }

  get hass() {
    return this._hass;
  }

  getCardSize() {
    return Math.ceil((this._config?.height || 400) / 50) + (this._config?.title ? 1 : 0);
  }

  connectedCallback() {
    if (this._config?.refresh_seconds > 0 && !this._timer) {
      this._timer = setInterval(() => this._refresh(), this._config.refresh_seconds * 1000);
    }
  }

  disconnectedCallback() {
    tokenListeners.delete(this);
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  _render() {
    const c = this._config;
    // Tear down before rewriting the shadow DOM: Leaflet must still find its
    // own container, otherwise its listeners stay on the discarded element.
    if (this._map) this._map.remove();
    this._map = null;
    this._mapInit = null;
    this._layers = null;
    this._flightLayers = {};
    this._tileLayer = null;
    this._tileUrl = null;
    this._spotLayer = null;
    this._spotMarkers = {};
    this._dipulLayer = null;
    this._hintEl = null;
    this._planMarker = null;
    tokenListeners.delete(this);
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="${STATIC}/leaflet.css">
      <style>
        :host { display: block; }
        ha-card { overflow: hidden; }
        .header { padding: 12px 16px 0; font-size: 1.2em; font-weight: 500; display: flex; justify-content: space-between; align-items: baseline; }
        .header .sub { font-size: 0.75em; color: var(--secondary-text-color); font-weight: normal; }
        .header .actions { display: flex; align-items: center; gap: 8px; }
        .header button { background: none; border: none; cursor: pointer; color: var(--secondary-text-color); font-size: 1.1em; padding: 2px 4px; line-height: 1; }
        .header button:hover { color: var(--primary-text-color); }
        .header button.busy { animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        #map { height: ${Number(c.height)}px; width: 100%; background: var(--card-background-color, #fff); }
        .empty { padding: 24px 16px; color: var(--secondary-text-color); text-align: center; }
        .leaflet-container { font: inherit; }
        .leaflet-popup-content { margin: 10px 12px; line-height: 1.4; }
        .leaflet-popup-content b { display: block; margin-bottom: 4px; }
        .leaflet-popup-content .links a { margin-right: 8px; cursor: pointer; color: var(--primary-color); }
        .dark .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(0.9) contrast(0.9); }
        .dark .leaflet-container { background: #111; }
        .leaflet-popup-content-wrapper, .leaflet-popup-tip { background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); }
        .leaflet-popup-content .zones { margin: 6px 0; }
        .leaflet-popup-content .zones ul { margin: 2px 0 0; padding-left: 18px; }
        .leaflet-popup-content .zones b { display: inline; margin: 0; }
        .leaflet-popup-content .zones.ok { color: var(--success-color, #43a047); }
        .leaflet-popup-content .muted, .leaflet-popup-content .small { color: var(--secondary-text-color); }
        .leaflet-popup-content .small { font-size: 0.8em; margin-top: 6px; }
        .leaflet-popup-content .err { color: var(--error-color, #db4437); }
        .leaflet-popup-content .note { white-space: pre-wrap; margin: 4px 0; }
        .leaflet-popup-content input, .leaflet-popup-content textarea {
          width: 100%; box-sizing: border-box; font: inherit; margin: 3px 0; padding: 5px 7px;
          border: 1px solid var(--divider-color, #ccc); border-radius: 6px;
          background: var(--card-background-color, #fff); color: inherit;
        }
        .leaflet-popup-content .btns { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 6px; }
        .leaflet-popup-content .btns button, .leaflet-popup-content .btns a {
          font: inherit; font-size: 0.9em; padding: 5px 10px; border-radius: 6px; cursor: pointer; text-decoration: none;
          border: 1px solid var(--primary-color, #03a9f4); background: none; color: var(--primary-color, #03a9f4); margin: 0;
        }
        .leaflet-popup-content .btns .primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); }
        .leaflet-popup-content .btns button:disabled { opacity: 0.5; cursor: default; }
        .dji-spot { background: none; border: none; }
        .planning .leaflet-container { cursor: crosshair; }
        .maphint {
          background: var(--card-background-color, #fff); color: var(--primary-text-color, #000);
          padding: 6px 10px; border-radius: 8px; font-size: 13px; box-shadow: 0 1px 4px rgba(0,0,0,.3);
          max-width: 60vw;
        }
      </style>
      <ha-card>
        ${c.title ? `<div class="header"><span>${esc(c.title)}</span><span class="actions"><span class="sub" id="sub"></span>${c.scan_button ? `<button id="scan" title="Log-Ordner jetzt scannen">&#x21bb;</button>` : ""}</span></div>` : ""}
        <div id="map"></div>
        <div class="empty" id="empty" hidden>Noch keine Flüge importiert.</div>
      </ha-card>`;
    const scan = this.shadowRoot.getElementById("scan");
    if (scan) scan.onclick = () => this._scanNow();
  }

  async _scanNow() {
    const btn = this.shadowRoot.getElementById("scan");
    if (!this._hass || !btn || btn.classList.contains("busy")) return;
    btn.classList.add("busy");
    try {
      await this._hass.callService("dji_flightlog", "scan", {});
      // The scan runs in the background; give it a moment, then reload.
      setTimeout(() => this._refresh(), 3000);
    } catch (err) {
      console.error("dji-flight-map-card scan:", err);
    } finally {
      setTimeout(() => btn.classList.remove("busy"), 3000);
    }
  }

  _isDark() {
    const d = this._config.dark;
    if (d === true || d === false) return d;
    return !!(this._hass?.themes?.darkMode);
  }

  _ensureMap() {
    if (this._map) return Promise.resolve(this._map);
    // Setup has several await points (Leaflet script, tile token). Without the
    // remembered promise a second call during that window would build a second
    // map into the same container.
    this._mapInit ??= this._initMap().catch((err) => {
      this._mapInit = null;
      throw err;
    });
    return this._mapInit;
  }

  async _initMap() {
    const L = await loadLeaflet();
    const el = this.shadowRoot.getElementById("map");
    if (!el) return null;
    const tiles = TILES[this._config.tiles] || TILES.ha;
    // Fetch the token before creating the map: from here on there is no await
    // left where anyone could get hold of a map that has no view yet.
    if (tiles.token && tileToken === null) await fetchTileToken(this._hass);
    const map = L.map(el, { zoomControl: true, attributionControl: true });
    // View first: without a center and zoom Leaflet has no pixel origin, and
    // every addLayer dies with "Cannot read properties of undefined
    // (reading 'x')".
    map.setView([51.0, 10.0], 5);
    this._map = map;
    this._layers = L.layerGroup().addTo(map);
    tokenListeners.add(this);
    ensureTokenRefresh(this._hass, () => tokenListeners.forEach((c) => c._applyTiles(true)));
    this._applyTiles();
    this._spotLayer = L.layerGroup().addTo(map);
    // Own pane above the base tiles: outside .leaflet-tile-pane, so the dark
    // mode filter does not invert the zone colours.
    const pane = map.createPane("dipul");
    pane.style.zIndex = 350;
    pane.style.pointerEvents = "none";
    const Hint = L.Control.extend({
      onAdd: () => {
        this._hintEl = L.DomUtil.create("div", "maphint");
        this._hintEl.hidden = true;
        return this._hintEl;
      },
    });
    new Hint({ position: "topright" }).addTo(map);
    map.on("click", (e) => this._onMapClick(e));
    map.on("zoomend", () => this._updateHint());
    this._applyDipul();
    // The card can be created while hidden (e.g. in a tab); Leaflet needs a size.
    new ResizeObserver(() => this._map && this._map.invalidateSize()).observe(el);
    return map;
  }

  _applyTiles(force = false) {
    const L = window.L;
    let tiles = TILES[this._config.tiles] || TILES.ha;
    if (tiles.token && !tileToken) tiles = TILES.carto; // older HA without map_tiles
    const dark = this._isDark() && !!tiles.dark;
    const url = dark ? tiles.dark : tiles.url;
    if (!force && this._tileLayer && this._tileUrl === url) return;
    if (this._tileLayer) this._map.removeLayer(this._tileLayer);
    this._tileUrl = url;
    this._tileLayer = L.tileLayer(url, {
      attribution: tiles.attribution,
      maxZoom: tiles.maxZoom,
      maxNativeZoom: tiles.maxNativeZoom || tiles.maxZoom,
      token: tileToken || "",
    }).addTo(this._map);
    // Providers without a dark variant get the same CSS filter HA's map uses.
    this.shadowRoot.querySelector("ha-card").classList.toggle("dark", this._isDark() && !tiles.dark);
  }

  _query() {
    const c = this._config;
    const q = new URLSearchParams();
    if (c.mode === "flight") q.set("ids", c.flight_id);
    if (c.mode === "last") q.set("limit", "1");
    if (c.limit && c.mode === "all") q.set("limit", String(c.limit));
    if (c.aircraft) q.set("aircraft", c.aircraft);
    if (c.since) q.set("since", c.since);
    else if (c.days) q.set("since", new Date(Date.now() - c.days * 86400e3).toISOString());
    return q;
  }

  async _refresh() {
    if (!this._hass || !this._config) return;
    // Queue instead of dropping: otherwise a filter change made while a load
    // is in flight is lost.
    if (this._loading) {
      this._pending = true;
      return;
    }
    this._loading = true;
    try {
      const q = this._query();
      const flightsRes = await this._hass.callApi("GET", `${API}/flights?${q}`);
      const flights = flightsRes.flights || [];
      const tq = new URLSearchParams(q);
      if (this._config.mode === "all" && this._config.max_points) {
        tq.set("max_points", String(this._config.max_points));
      }
      const tracksRes = flights.length ? await this._hass.callApi("GET", `${API}/tracks?${tq}`) : { tracks: [] };
      await this._draw(flights, tracksRes.tracks || [], flightsRes);
    } catch (err) {
      console.error("dji-flight-map-card:", err);
      const sub = this.shadowRoot.getElementById("sub");
      if (sub) sub.textContent = `Fehler: ${err.message || err}`;
    } finally {
      this._loading = false;
      if (this._pending) {
        this._pending = false;
        this._refresh();
      }
    }
  }

  async _draw(flights, tracks, meta) {
    const map = await this._ensureMap();
    if (!map) return;
    const L = window.L;
    const c = this._config;
    this._applyTiles();
    this._layers.clearLayers();
    this._flightLayers = {};
    this._hasFlights = flights.length > 0;

    const empty = this.shadowRoot.getElementById("empty");
    empty.hidden = flights.length > 0;
    const sub = this.shadowRoot.getElementById("sub");
    if (sub) {
      const t = meta.totals || {};
      sub.textContent =
        c.mode === "all"
          ? `${flights.length} Flüge · ${fmtDur(flights.reduce((a, f) => a + (f.duration_s || 0), 0))} · ${fmtDist(flights.reduce((a, f) => a + (f.distance_m || 0), 0))}`
          : flights[0]
            ? `${fmtDate(flights[0].start_time)} · ${fmtDur(flights[0].duration_s)} · ${fmtDist(flights[0].distance_m)}`
            : `${t.flights || 0} Flüge`;
    }
    if (!flights.length) return;

    const byId = Object.fromEntries(tracks.map((t) => [t.flight_id, t]));
    const colorFor = this._colorFn(flights);
    const bounds = L.latLngBounds([]);
    const heatPoints = [];

    for (const f of flights) {
      const color = colorFor(f);
      const track = byId[f.flight_id];
      const popup = this._popupHtml(f);
      const entry = (this._flightLayers[f.flight_id] = {
        color,
        bounds: L.latLngBounds([]),
      });
      if (track && track.points.length > 1) {
        const latlngs = track.points.map((p) => [p[1], p[0]]);
        const line = L.polyline(latlngs, { color, weight: c.line_weight, opacity: 0.85 });
        line.bindPopup(popup);
        line.on("popupopen", (e) => this._wirePopup(e.popup, f));
        this._layers.addLayer(line);
        entry.line = line;
        latlngs.forEach((ll) => {
          bounds.extend(ll);
          entry.bounds.extend(ll);
        });
        if (c.heatmap) for (const ll of latlngs) heatPoints.push([ll[0], ll[1], 0.5]);
        if (c.home && track.home) {
          this._layers.addLayer(
            L.circleMarker([track.home[1], track.home[0]], { radius: 5, color: "#fff", fillColor: "#222", fillOpacity: 1, weight: 2 }).bindTooltip("Home")
          );
        }
      }
      if (c.markers && f.takeoff_lat != null && f.takeoff_lon != null) {
        const m = L.circleMarker([f.takeoff_lat, f.takeoff_lon], { radius: 6, color: "#fff", fillColor: color, fillOpacity: 1, weight: 2 });
        m.bindPopup(popup);
        m.on("popupopen", (e) => this._wirePopup(e.popup, f));
        this._layers.addLayer(m);
        entry.marker = m;
        entry.bounds.extend([f.takeoff_lat, f.takeoff_lon]);
        bounds.extend([f.takeoff_lat, f.takeoff_lon]);
      }
    }

    if (c.heatmap && heatPoints.length) {
      this._layers.addLayer(L.heatLayer(heatPoints, { radius: 18, blur: 20, minOpacity: 0.3 }));
    }

    if (this._focused && this._flightLayers[this._focused]) {
      this.focusFlight(this._focused, { openPopup: false });
    } else if (c.fit && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [24, 24], maxZoom: 17 });
    }
    setTimeout(() => map.invalidateSize(), 50);
  }

  /** Merge config changes (filters, mode) and reload without rebuilding the map. */
  updateOptions(patch) {
    Object.assign(this._config, patch);
    this._lastRefreshKey = null;
    return this._refresh();
  }

  /** Zoom to one flight and highlight it; pass null to clear the selection. */
  focusFlight(flightId, { openPopup = true } = {}) {
    this._focused = flightId;
    const entry = flightId && this._flightLayers[flightId];
    for (const [id, l] of Object.entries(this._flightLayers)) {
      const on = !flightId || id === flightId;
      if (l.line) l.line.setStyle({ opacity: on ? 0.95 : 0.25, weight: this._config.line_weight + (id === flightId ? 2 : 0) });
      if (l.marker) l.marker.setStyle({ opacity: on ? 1 : 0.3, fillOpacity: on ? 1 : 0.3 });
    }
    if (!entry || !this._map) return;
    if (entry.bounds && entry.bounds.isValid()) {
      this._map.fitBounds(entry.bounds, { padding: [40, 40], maxZoom: 17 });
    }
    if (openPopup) (entry.line || entry.marker)?.openPopup();
  }

  // -- DIPUL overlay and saved spots -------------------------------------

  /** Show or hide the DIPUL geo zones. */
  setDipul(on) {
    this._config.dipul = !!on;
    this._applyDipul();
  }

  /** Planning mode: DIPUL zones on, a click on the map picks a new spot. */
  setPlanning(on) {
    this._planning = !!on;
    this.shadowRoot.querySelector("ha-card")?.classList.toggle("planning", this._planning);
    if (!this._planning && this._planMarker) {
      this._planMarker.remove();
      this._planMarker = null;
    }
    this._ensureMap().then(() => this._applyDipul());
  }

  /** Zoom to a saved spot and open its popup (waits for the spots if needed). */
  focusSpot(id) {
    const marker = this._spotMarkers[id];
    if (!marker || !this._map) {
      this._pendingSpot = id;
      return;
    }
    this._pendingSpot = null;
    this._map.setView(marker.getLatLng(), Math.max(this._map.getZoom(), 14));
    marker.openPopup();
  }

  async reloadSpots() {
    if (!this._hass || !this._config?.spots) return;
    try {
      const res = await this._hass.callApi("GET", `${API}/spots`);
      this._spots = res.spots || [];
      await this._drawSpots();
    } catch (err) {
      console.error("dji-flight-map-card spots:", err);
    }
  }

  _applyDipul() {
    if (!this._map) return;
    const on = !!(this._config.dipul || this._planning);
    if (on && !this._dipulLayer) {
      this._dipulLayer = window.L.tileLayer.wms(DIPUL_WMS, {
        layers: DIPUL_QUERY,
        styles: "",
        format: "image/png",
        transparent: true,
        version: "1.3.0",
        tileSize: 512,
        opacity: 0.75,
        pane: "dipul",
        minZoom: DIPUL_MIN_ZOOM,
        maxZoom: 20,
        attribution: DIPUL_ATTR,
      });
    }
    if (this._dipulLayer) {
      if (on) this._dipulLayer.addTo(this._map);
      else this._dipulLayer.remove();
    }
    this._updateHint();
  }

  _updateHint() {
    if (!this._hintEl || !this._map) return;
    const msgs = [];
    if ((this._config.dipul || this._planning) && this._map.getZoom() < DIPUL_MIN_ZOOM) {
      msgs.push("Für die DIPUL-Zonen hineinzoomen.");
    }
    if (this._planning) msgs.push("Auf die Karte tippen, um einen Ort zu merken.");
    this._hintEl.textContent = msgs.join(" ");
    this._hintEl.hidden = !msgs.length;
  }

  async _drawSpots() {
    const map = await this._ensureMap();
    if (!map) return;
    const L = window.L;
    this._spotLayer.clearLayers();
    this._spotMarkers = {};
    for (const s of this._spots) {
      const marker = L.marker([s.lat, s.lon], { icon: spotIcon(L), title: s.name });
      marker.bindPopup(() => this._spotPopup(s), { minWidth: 220, maxWidth: 320 });
      this._spotLayer.addLayer(marker);
      this._spotMarkers[s.id] = marker;
    }
    if (this._pendingSpot) {
      this.focusSpot(this._pendingSpot);
    } else if (!this._hasFlights && this._config.fit && this._spots.length && !this._spotsFitted) {
      this._spotsFitted = true;
      map.fitBounds(L.latLngBounds(this._spots.map((s) => [s.lat, s.lon])), { padding: [40, 40], maxZoom: 14 });
    }
  }

  _spotsChanged() {
    this.dispatchEvent(new CustomEvent("dji-spots-changed", { bubbles: true, composed: true }));
    return this.reloadSpots();
  }

  _spotPopup(s) {
    const el = document.createElement("div");
    el.innerHTML = `
      <b>${esc(s.name)}</b>
      ${s.note ? `<div class="note">${esc(s.note)}</div>` : ""}
      <div class="zwrap">${zonesHtml(s.zones, s.zones_checked)}</div>
      <div class="btns">
        <a class="primary" href="${esc(s.maps_url)}" target="_blank" rel="noopener">Navigation</a>
        <button class="recheck">Zonen prüfen</button>
        <button class="del">Löschen</button>
      </div>
      <div class="err" hidden></div>
      <div class="small">Gemerkt am ${esc(fmtDate(s.created))} · ${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}</div>`;
    const err = el.querySelector(".err");
    const fail = (e) => {
      err.textContent = e.body?.message || e.message || String(e);
      err.hidden = false;
    };
    const recheck = el.querySelector(".recheck");
    recheck.onclick = async () => {
      recheck.disabled = true;
      try {
        const zones = await queryDipul(s.lat, s.lon);
        await this._hass.callApi("PATCH", `${API}/spots/${s.id}`, { zones, zones_checked: new Date().toISOString() });
        this._pendingSpot = s.id; // reopen the popup after the redraw
        await this._spotsChanged();
      } catch (e) {
        recheck.disabled = false;
        fail(e);
      }
    };
    el.querySelector(".del").onclick = async () => {
      if (!confirm(`„${s.name}“ löschen?`)) return;
      try {
        await this._hass.callApi("DELETE", `${API}/spots/${s.id}`);
        await this._spotsChanged();
      } catch (e) {
        fail(e);
      }
    };
    return el;
  }

  _onMapClick(e) {
    if (!this._planning) return;
    const L = window.L;
    const { lat, lng } = e.latlng;
    if (this._planMarker) this._planMarker.setLatLng(e.latlng);
    else this._planMarker = L.marker(e.latlng, { icon: spotIcon(L, PLAN_COLOR), zIndexOffset: 1000 }).addTo(this._map);

    const el = document.createElement("div");
    el.innerHTML = `
      <b>Neuer Ort</b>
      <div class="zwrap"><div class="zones muted">Prüfe DIPUL-Zonen …</div></div>
      <input class="name" placeholder="Name, z. B. Feld am Waldrand" maxlength="100">
      <textarea class="note" rows="2" placeholder="Notiz (optional)" maxlength="1000"></textarea>
      <div class="btns">
        <button class="save primary">Merken</button>
        <a href="${esc(mapsUrl(lat, lng))}" target="_blank" rel="noopener">Navigation</a>
      </div>
      <div class="err" hidden></div>
      <div class="small">${lat.toFixed(5)}, ${lng.toFixed(5)} · Zonen nur zur Orientierung, vor dem Flug auf dipul.de prüfen.</div>`;
    this._planMarker.unbindPopup().bindPopup(el, { minWidth: 240, maxWidth: 320 }).openPopup();

    const seq = ++this._planSeq;
    const popup = this._planMarker.getPopup();
    const showZones = (html) => {
      if (seq !== this._planSeq) return; // a newer click replaced this popup
      el.querySelector(".zwrap").innerHTML = html;
      popup.update();
    };
    const zonesP = queryDipul(lat, lng).then(
      (zones) => {
        const checked = new Date().toISOString();
        showZones(zonesHtml(zones, checked));
        return { zones, zones_checked: checked };
      },
      (err) => {
        showZones(`<div class="zones muted">${esc(err.message || err)}. Der Ort kann trotzdem gemerkt werden.</div>`);
        return { zones: null, zones_checked: null };
      },
    );

    const name = el.querySelector(".name");
    const save = el.querySelector(".save");
    const submit = async () => {
      if (!name.value.trim()) {
        name.focus();
        return;
      }
      save.disabled = true;
      try {
        const res = await this._hass.callApi("POST", `${API}/spots`, {
          name: name.value.trim(),
          lat,
          lon: lng,
          note: el.querySelector(".note").value,
          ...(await zonesP),
        });
        this._planMarker?.remove();
        this._planMarker = null;
        this._pendingSpot = res.spot?.id || null;
        await this._spotsChanged();
      } catch (err) {
        save.disabled = false;
        const box = el.querySelector(".err");
        box.textContent = err.body?.message || err.message || String(err);
        box.hidden = false;
      }
    };
    save.onclick = submit;
    // Block body: an on* handler returning false would cancel every keystroke.
    name.onkeydown = (ev) => {
      if (ev.key === "Enter") submit();
    };
    setTimeout(() => name.focus(), 50);
  }


  _colorFn(flights) {
    if (this._config.line_color) return () => this._config.line_color;
    const sns = [...new Set(flights.map((f) => f.aircraft_sn || "?"))];
    if (sns.length <= 1 && this._config.mode === "all") {
      // Single aircraft: colour by flight so overlapping tracks stay readable.
      const ids = flights.map((f) => f.flight_id);
      return (f) => PALETTE[ids.indexOf(f.flight_id) % PALETTE.length];
    }
    return (f) => PALETTE[sns.indexOf(f.aircraft_sn || "?") % PALETTE.length];
  }

  _popupHtml(f) {
    const rows = [
      ["Dauer", fmtDur(f.duration_s)],
      ["Distanz", fmtDist(f.distance_m)],
      ["Max. Höhe", `${Math.round(f.max_height_m || 0)} m`],
      ["Max. Speed", `${((f.max_h_speed_ms || 0) * 3.6).toFixed(1)} km/h`],
    ];
    if (f.battery_start_pct != null && f.battery_end_pct != null) rows.push(["Akku", `${f.battery_start_pct}% → ${f.battery_end_pct}%`]);
    if (f.city) rows.push(["Ort", f.city]);
    const exports = f.points
      ? `<div class="links">${["gpx", "kml", "geojson"].map((x) => `<a data-fmt="${x}">${x.toUpperCase()}</a>`).join("")}</div>`
      : `<div class="links"><i>kein Track (verschlüsseltes Log ohne API-Key)</i></div>`;
    return `<b>${esc(f.aircraft_name || f.product_type || "DJI")} · ${esc(fmtDate(f.start_time))}</b>
      ${rows.map(([k, v]) => `${esc(k)}: ${esc(v)}<br>`).join("")}${exports}`;
  }

  _wirePopup(popup, f) {
    const el = popup.getElement();
    if (!el) return;
    el.querySelectorAll("a[data-fmt]").forEach((a) => {
      a.onclick = async (ev) => {
        ev.preventDefault();
        const fmt = a.dataset.fmt;
        try {
          const res = await this._hass.fetchWithAuth(`/api/${API}/flights/${f.flight_id}/export/${fmt}`);
          if (!res.ok) throw new Error(`${res.status}`);
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = `${(f.start_time || "").slice(0, 10)}_${f.flight_id}.${fmt}`;
          document.body.appendChild(link);
          link.click();
          link.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (err) {
          console.error("dji-flight-map-card export:", err);
        }
      };
    });
  }
}

/**
 * List of saved spots for a dashboard: name, note, DIPUL zones and a Google
 * Maps link that starts navigation on a phone. Tapping a spot opens it in
 * the sidebar panel.
 */
class DjiSpotsCard extends HTMLElement {
  static getStubConfig() {
    return { title: "Gemerkte Orte" };
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = null;
    this._spots = null;
    this._key = null;
  }

  setConfig(config) {
    this._config = {
      title: "Gemerkte Orte",
      entity: "sensor.dji_flight_log_saved_spots",
      zones: true,
      limit: null,
      panel_path: "/dji-flightlog", // null: rows are not clickable
      ...config,
    };
    this._render();
    if (this._spots) this._renderList();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const ent = hass.states[this._config?.entity];
    const key = ent ? ent.last_updated : "none";
    if (first || key !== this._key) {
      this._key = key;
      this._load();
    }
  }

  getCardSize() {
    return 1 + Math.min(this._spots?.length || 1, 6);
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        .header { padding: 12px 16px 4px; font-size: 1.2em; font-weight: 500; }
        .row { display: flex; align-items: center; gap: 12px; padding: 8px 16px; }
        .row.link { cursor: pointer; }
        .row.link:hover { background: var(--secondary-background-color, #f2f2f2); }
        .pin { flex: 0 0 auto; color: ${SPOT_COLOR}; line-height: 0; }
        .main { flex: 1; min-width: 0; }
        .n { font-size: 14px; }
        .d { font-size: 12px; color: var(--secondary-text-color); white-space: pre-wrap; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
        .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 3px; }
        .chip { font-size: 11px; padding: 1px 7px; border-radius: 10px; background: var(--secondary-background-color, #eee); color: var(--primary-text-color); }
        .chip.ok { background: none; color: var(--success-color, #43a047); padding-left: 0; }
        .nav {
          flex: 0 0 auto; display: inline-flex; align-items: center; gap: 4px; text-decoration: none;
          color: var(--primary-color, #03a9f4); font-size: 13px; padding: 6px 8px; border-radius: 8px;
        }
        .nav:hover { background: var(--secondary-background-color, #f2f2f2); }
        .empty { padding: 8px 16px 16px; color: var(--secondary-text-color); font-size: 14px; }
        .list { padding-bottom: 8px; }
      </style>
      <ha-card>
        ${this._config.title ? `<div class="header">${esc(this._config.title)}</div>` : ""}
        <div class="list" id="list"></div>
      </ha-card>`;
  }

  async _load() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callApi("GET", `${API}/spots`);
      this._spots = res.spots || [];
    } catch (err) {
      console.error("dji-spots-card:", err);
      const msg = err.body?.message || err.message || err.error || err.status_code || err;
      this.shadowRoot.getElementById("list").innerHTML = `<div class="empty">Fehler: ${esc(msg)}</div>`;
      return;
    }
    this._renderList();
  }

  _renderList() {
    const list = this.shadowRoot.getElementById("list");
    let spots = this._spots || [];
    if (this._config.limit) spots = spots.slice(0, Number(this._config.limit));
    if (!spots.length) {
      list.innerHTML = `<div class="empty">Noch keine Orte gemerkt. Im Panel „Drohnenflüge“ auf „Ort merken“ tippen und einen Punkt auf der Karte wählen.</div>`;
      return;
    }
    const link = !!this._config.panel_path;
    list.innerHTML = spots
      .map(
        (s) => `
        <div class="row${link ? " link" : ""}" data-id="${esc(s.id)}">
          <div class="pin"><svg viewBox="0 0 24 24" width="24" height="24"><path d="${PIN_PATH}" fill="currentColor"/><circle cx="12" cy="9" r="2.6" fill="#fff"/></svg></div>
          <div class="main">
            <div class="n">${esc(s.name)}</div>
            ${s.note ? `<div class="d">${esc(s.note)}</div>` : ""}
            ${this._config.zones ? zoneChips(s.zones) : ""}
          </div>
          <a class="nav" href="${esc(s.maps_url)}" target="_blank" rel="noopener" title="Navigation mit Google Maps starten">
            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12,2L4.5,20.29L5.21,21L12,18L18.79,21L19.5,20.29L12,2Z"/></svg>Route
          </a>
        </div>`,
      )
      .join("");
    for (const a of list.querySelectorAll("a.nav")) a.onclick = (ev) => ev.stopPropagation();
    if (!link) return;
    for (const row of list.querySelectorAll(".row")) {
      row.onclick = () => {
        history.pushState(null, "", `${this._config.panel_path}?spot=${encodeURIComponent(row.dataset.id)}`);
        window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
      };
    }
  }
}

function zoneChips(zones) {
  if (zones == null) return "";
  if (!zones.length) return `<div class="chips"><span class="chip ok">keine DIPUL-Zone</span></div>`;
  const labels = [...new Set(zones.map(zoneLabel))];
  return `<div class="chips">${labels.map((l) => `<span class="chip">${esc(l)}</span>`).join("")}</div>`;
}

if (!customElements.get("dji-flight-map-card")) {
  customElements.define("dji-flight-map-card", DjiFlightMapCard);
}
if (!customElements.get("dji-spots-card")) {
  customElements.define("dji-spots-card", DjiSpotsCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "dji-flight-map-card")) {
  window.customCards.push({
    type: "dji-flight-map-card",
    name: "DJI Flight Map",
    description: "Tracks of imported DJI flights on a Leaflet map",
    preview: false,
  });
}
if (!window.customCards.some((c) => c.type === "dji-spots-card")) {
  window.customCards.push({
    type: "dji-spots-card",
    name: "DJI Gemerkte Orte",
    description: "Saved drone spots with DIPUL zones and a Google Maps navigation link",
    preview: false,
  });
}
console.info(`%c DJI-FLIGHT-MAP-CARD %c ${CARD_VERSION} `, "color: white; background: #222; font-weight: bold", "color: #222; background: #ddd");
