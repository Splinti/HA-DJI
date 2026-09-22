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
 */

const STATIC = "/dji_flightlog_static";
const API = "dji_flightlog";
const CARD_VERSION = "0.1.0";

const PALETTE = [
  "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
  "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
];

// Same tile provider Home Assistant's own map card uses. OpenStreetMap's
// tile servers reject requests without a Referer, which HA never sends.
const CARTO_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';
const TILES = {
  osm: {
    url: "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    dark: "https://basemaps.cartocdn.com/rastertiles/dark_all/{z}/{x}/{y}{r}.png",
    attribution: CARTO_ATTR,
    maxZoom: 20,
  },
  light: {
    url: "https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}{r}.png",
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
    this._tileLayer = null;
    this._tileUrl = null;
    this._lastRefreshKey = null;
    this._loading = false;
    this._timer = null;
  }

  setConfig(config) {
    if (config.mode === "flight" && !config.flight_id) {
      throw new Error("mode: flight requires flight_id");
    }
    this._config = {
      title: "",
      mode: "all",
      height: 400,
      tiles: "osm",
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
      ...config,
    };
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const ent = hass.states[this._config?.refresh_entity];
    const key = ent ? ent.state : "none";
    if (first || key !== this._lastRefreshKey) {
      this._lastRefreshKey = key;
      this._refresh();
    }
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
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  _render() {
    const c = this._config;
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="${STATIC}/leaflet.css">
      <style>
        :host { display: block; }
        ha-card { overflow: hidden; }
        .header { padding: 12px 16px 0; font-size: 1.2em; font-weight: 500; display: flex; justify-content: space-between; align-items: baseline; }
        .header .sub { font-size: 0.75em; color: var(--secondary-text-color); font-weight: normal; }
        #map { height: ${Number(c.height)}px; width: 100%; background: var(--card-background-color, #fff); }
        .empty { padding: 24px 16px; color: var(--secondary-text-color); text-align: center; }
        .leaflet-container { font: inherit; }
        .leaflet-popup-content { margin: 10px 12px; line-height: 1.4; }
        .leaflet-popup-content b { display: block; margin-bottom: 4px; }
        .leaflet-popup-content .links a { margin-right: 8px; cursor: pointer; color: var(--primary-color); }
        .dark .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(0.9) contrast(0.9); }
        .dark .leaflet-container { background: #111; }
        .leaflet-popup-content-wrapper, .leaflet-popup-tip { background: var(--card-background-color, #fff); color: var(--primary-text-color, #000); }
      </style>
      <ha-card>
        ${c.title ? `<div class="header"><span>${esc(c.title)}</span><span class="sub" id="sub"></span></div>` : ""}
        <div id="map"></div>
        <div class="empty" id="empty" hidden>Noch keine Flüge importiert.</div>
      </ha-card>`;
    this._map = null;
    this._tileLayer = null;
    this._tileUrl = null;
  }

  _isDark() {
    const d = this._config.dark;
    if (d === true || d === false) return d;
    return !!(this._hass?.themes?.darkMode);
  }

  async _ensureMap() {
    const L = await loadLeaflet();
    if (this._map) return this._map;
    const el = this.shadowRoot.getElementById("map");
    if (!el) return null;
    this._map = L.map(el, { zoomControl: true, attributionControl: true });
    this._applyTiles();
    this._map.setView([51.0, 10.0], 5);
    this._layers = L.layerGroup().addTo(this._map);
    // The card can be created while hidden (e.g. in a tab); Leaflet needs a size.
    new ResizeObserver(() => this._map && this._map.invalidateSize()).observe(el);
    return this._map;
  }

  _applyTiles() {
    const L = window.L;
    const tiles = TILES[this._config.tiles] || TILES.osm;
    const dark = this._isDark() && !!tiles.dark;
    const url = dark ? tiles.dark : tiles.url;
    if (this._tileLayer && this._tileUrl === url) return;
    if (this._tileLayer) this._map.removeLayer(this._tileLayer);
    this._tileUrl = url;
    this._tileLayer = L.tileLayer(url, { attribution: tiles.attribution, maxZoom: tiles.maxZoom }).addTo(this._map);
    // Only providers without a dark variant get the CSS invert.
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
    if (!this._hass || !this._config || this._loading) return;
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
    }
  }

  async _draw(flights, tracks, meta) {
    const map = await this._ensureMap();
    if (!map) return;
    const L = window.L;
    const c = this._config;
    this._applyTiles();
    this._layers.clearLayers();

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
      if (track && track.points.length > 1) {
        const latlngs = track.points.map((p) => [p[1], p[0]]);
        const line = L.polyline(latlngs, { color, weight: c.line_weight, opacity: 0.85 });
        line.bindPopup(popup);
        line.on("popupopen", (e) => this._wirePopup(e.popup, f));
        this._layers.addLayer(line);
        latlngs.forEach((ll) => bounds.extend(ll));
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
        bounds.extend([f.takeoff_lat, f.takeoff_lon]);
      }
    }

    if (c.heatmap && heatPoints.length) {
      this._layers.addLayer(L.heatLayer(heatPoints, { radius: 18, blur: 20, minOpacity: 0.3 }));
    }

    if (c.fit && bounds.isValid()) {
      map.fitBounds(bounds, { padding: [24, 24], maxZoom: 17 });
    }
    setTimeout(() => map.invalidateSize(), 50);
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

if (!customElements.get("dji-flight-map-card")) {
  customElements.define("dji-flight-map-card", DjiFlightMapCard);
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
console.info(`%c DJI-FLIGHT-MAP-CARD %c ${CARD_VERSION} `, "color: white; background: #222; font-weight: bold", "color: #222; background: #ddd");
