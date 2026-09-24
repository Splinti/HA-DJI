/*
 * dji-flight-details
 *
 * Detail view of one flight for the dji_flightlog panel: key figures, the
 * track on a map, charts over the flight time (height, speed, distance from
 * home, battery, battery temperature) with the flight modes and flight
 * controller events, and the battery / recording / technical data.
 *
 * The panel creates it, sets `hass`, hands over the (filtered) flight list
 * with `setFlights()` and picks one with `show(flightId)`. Prev/next inside
 * the view fire `dji-flight-selected` so the panel can follow.
 *
 * Charts are plain SVG: one sample per second from the track file's
 * `profile`, no chart library.
 */

const STATIC = "/dji_flightlog_static";
const API = "dji_flightlog";
const VERSION_QUERY = new URL(import.meta.url).search;

// The map card module also holds the labels for modes and actions.
let cardModule = null;
const loadCardModule = () => (cardModule ??= import(`${STATIC}/dji-flight-map-card.js${VERSION_QUERY}`));

const fmtDate = (iso, opts = { dateStyle: "full", timeStyle: "short" }) =>
  iso ? new Date(iso).toLocaleString(undefined, opts) : "";
const fmtClock = (s) => {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
const fmtDur = (s) => {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m}:${String(s % 60).padStart(2, "0")} min`;
};
const fmtDist = (m) => (m == null ? "–" : m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`);
const fmtNum = (v, digits = 0, unit = "") =>
  v == null || Number.isNaN(v) ? "–" : `${Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}${unit ? ` ${unit}` : ""}`;
const esc = (t) =>
  String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const CHARTS = [
  { key: "height", label: "Höhe über Start", unit: "m", color: "#4363d8", digits: 0, floor: 0 },
  { key: "speed", label: "Geschwindigkeit", unit: "km/h", color: "#f58231", digits: 0, scale: 3.6, floor: 0 },
  { key: "dist", label: "Entfernung vom Home-Punkt", unit: "m", color: "#3cb44b", digits: 0, floor: 0 },
  { key: "battery", label: "Akku", unit: "%", color: "#e6194b", digits: 0, range: [0, 100] },
  { key: "temp", label: "Akku-Temperatur", unit: "°C", color: "#911eb4", digits: 1 },
];

// Colour per flight mode label (see MODE_LABELS in the map card).
const MODE_COLORS = {
  Normal: "#78909c",
  Sport: "#f58231",
  Cine: "#3cb44b",
  Anfänger: "#8bc34a",
  ActiveTrack: "#911eb4",
  "Rückkehr (RTH)": "#e6194b",
  Manuell: "#f032e6",
};
const OTHER_MODE_COLOR = "#bdbdbd";

const PAD_L = 44;
const PAD_R = 10;
const CHART_H = 86;

class DjiFlightDetails extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._flights = [];
    this._id = null;
    this._flight = null;
    this._track = null;
    this._labels = null;
    this._seq = 0;
    this._width = 0;
    this._hover = null;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const map = this._map;
    if (map && this._mapReady) map.hass = hass;
    if (this._pendingId && hass) this.show(this._pendingId);
  }

  get hass() {
    return this._hass;
  }

  /** The flights prev/next walks through (newest first, like the panel list). */
  setFlights(flights) {
    this._flights = flights || [];
    if (this._flight) {
      // Summaries change after an import or re-parse; keep the shown one current.
      const fresh = this._flights.find((f) => f.flight_id === this._id);
      if (fresh) this._flight = fresh;
    }
    this._renderHead();
  }

  async show(flightId) {
    if (!this._hass) {
      this._pendingId = flightId;
      return;
    }
    this._pendingId = null;
    if (!flightId) {
      this._id = null;
      this._flight = null;
      this._track = null;
      this._renderAll();
      return;
    }
    const seq = ++this._seq;
    this._id = flightId;
    this._flight = this._flights.find((f) => f.flight_id === flightId) || null;
    this._track = null;
    this._renderAll({ loading: true });
    this._showMap(flightId);
    const [labels, res] = await Promise.all([
      loadCardModule().catch(() => null),
      this._hass.callApi("GET", `${API}/flights/${flightId}/track`).catch(() => null),
    ]);
    if (seq !== this._seq) return; // another flight was picked meanwhile
    this._labels = labels;
    if (res) {
      this._flight = res.flight || this._flight;
      this._track = res;
    }
    this._renderAll();
  }

  // -- layout ---------------------------------------------------------------

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; color: var(--primary-text-color); }
        .wrap { padding: 12px 16px 24px; display: flex; flex-direction: column; gap: 12px; }
        .head { display: flex; align-items: center; gap: 8px; }
        .head .title { flex: 1; min-width: 0; }
        .head h2 { margin: 0; font-size: 18px; font-weight: 500; }
        .head .sub { font-size: 13px; color: var(--secondary-text-color); }
        .head button {
          background: none; border: 1px solid var(--divider-color, #e0e0e0); border-radius: 50%;
          width: 36px; height: 36px; cursor: pointer; color: inherit; font-size: 18px; line-height: 1;
        }
        .head button:disabled { opacity: 0.35; cursor: default; }
        .tiles { display: flex; flex-wrap: wrap; gap: 12px; }
        .tile, .card {
          background: var(--card-background-color, #fff);
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
        }
        .tile { flex: 1 1 120px; padding: 10px 14px; }
        .tile .v { font-size: 20px; font-weight: 500; white-space: nowrap; }
        .tile .k { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
        .banner { padding: 10px 14px; border-radius: 8px; font-size: 14px; color: #000; background: var(--warning-color, #ffa600); }
        .banner.crit { background: var(--error-color, #db4437); color: #fff; }
        .main { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 3fr); gap: 12px; }
        :host(.narrow) .main { grid-template-columns: minmax(0, 1fr); }
        .card { padding: 12px 14px; min-width: 0; }
        .card h3 { margin: 0 0 8px; font-size: 14px; font-weight: 500; }
        .mapcard { padding: 0; overflow: hidden; display: flex; min-height: 320px; isolation: isolate; }
        .mapcard dji-flight-map-card { flex: 1; display: block; min-width: 0; }
        .info { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        td { padding: 3px 0; vertical-align: top; }
        td:first-child { color: var(--secondary-text-color); padding-right: 12px; white-space: nowrap; }
        td.warn { color: var(--warning-color, #ffa600); }
        td.crit { color: var(--error-color, #db4437); }
        .muted { color: var(--secondary-text-color); font-size: 13px; }
        .links a { color: var(--primary-color); cursor: pointer; margin-right: 12px; font-size: 13px; }

        /* charts */
        .charts { position: relative; touch-action: pan-y; user-select: none; }
        .chart { margin-bottom: 6px; }
        .chart .lbl { display: flex; justify-content: space-between; font-size: 12px; color: var(--secondary-text-color); padding-left: ${PAD_L}px; }
        .chart .lbl b { color: var(--primary-text-color); font-weight: 500; }
        svg { display: block; overflow: visible; }
        svg text { fill: var(--secondary-text-color); font-size: 10px; font-family: inherit; }
        svg .grid { stroke: var(--divider-color, #e0e0e0); stroke-width: 1; }
        svg .cursor { stroke: var(--primary-text-color); stroke-width: 1; opacity: 0.6; }
        svg .event { stroke-width: 1; stroke-dasharray: 3 3; opacity: 0.8; }
        .band { margin: 2px 0 8px; }
        .legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; color: var(--secondary-text-color); padding-left: ${PAD_L}px; }
        .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; vertical-align: -1px; }
        .bars .row { display: grid; grid-template-columns: 110px 1fr 56px; align-items: center; gap: 8px; font-size: 13px; margin: 3px 0; }
        .bars .bar { height: 8px; border-radius: 4px; background: var(--secondary-background-color, #eee); overflow: hidden; }
        .bars .bar div { height: 100%; }
        .bars .t { text-align: right; color: var(--secondary-text-color); }
        .events div { font-size: 13px; padding: 2px 0; display: flex; gap: 10px; }
        .events .at { color: var(--secondary-text-color); width: 40px; flex: 0 0 auto; text-align: right; }
        .events .warn { color: var(--warning-color, #ffa600); }
        .events .crit { color: var(--error-color, #db4437); }
        .empty { padding: 32px 16px; text-align: center; color: var(--secondary-text-color); }
        .content { display: flex; flex-direction: column; gap: 12px; }
        .content[hidden], .empty[hidden] { display: none; }
        #banner { display: flex; flex-direction: column; gap: 8px; }
        #banner:empty { display: none; }
      </style>
      <div class="wrap">
        <div class="head" id="head"></div>
        <div class="empty" id="empty">Noch kein Flug ausgewählt.<br>In der Liste unter „Flüge“ einen Flug antippen.</div>
        <div class="content" id="content" hidden>
          <div class="tiles" id="tiles"></div>
          <div id="banner"></div>
          <div class="main">
            <!-- Built once: re-inserting the map would detach Leaflet and its tile token refresh. -->
            <div class="card mapcard"><dji-flight-map-card></dji-flight-map-card></div>
            <div class="card">
              <h3>Verlauf</h3>
              <div id="charts"></div>
            </div>
          </div>
          <div class="info" id="info"></div>
        </div>
      </div>`;
  }

  get _map() {
    return this.shadowRoot.querySelector("dji-flight-map-card");
  }

  _renderHead() {
    const head = this.shadowRoot.getElementById("head");
    const f = this._flight;
    if (!f) {
      head.innerHTML = "";
      return;
    }
    const i = this._flights.findIndex((x) => x.flight_id === this._id);
    // The list is newest first: "back" in time is the next entry.
    const older = i >= 0 ? this._flights[i + 1] : null;
    const newer = i > 0 ? this._flights[i - 1] : null;
    const place = [f.street, f.city].filter(Boolean).join(", ");
    head.innerHTML = `
      <button id="older" title="Vorheriger Flug" ${older ? "" : "disabled"}>‹</button>
      <div class="title">
        <h2>${esc(fmtDate(f.start_time))}</h2>
        <div class="sub">${esc(f.aircraft_name || "DJI")}${place ? ` · ${esc(place)}` : ""}</div>
      </div>
      <button id="newer" title="Nächster Flug" ${newer ? "" : "disabled"}>›</button>`;
    const go = (target) => {
      if (!target) return;
      this.dispatchEvent(
        new CustomEvent("dji-flight-selected", { detail: { flight_id: target.flight_id }, bubbles: true, composed: true }),
      );
      this.show(target.flight_id);
    };
    head.querySelector("#older").onclick = () => go(older);
    head.querySelector("#newer").onclick = () => go(newer);
  }

  _renderAll({ loading = false } = {}) {
    this._renderHead();
    const $ = (id) => this.shadowRoot.getElementById(id);
    const f = this._flight;
    $("empty").hidden = !!f;
    $("content").hidden = !f;
    if (!f) return;
    $("tiles").innerHTML = this._tilesHtml(f);
    $("banner").innerHTML = this._bannerHtml(f);
    $("info").innerHTML =
      this._modesHtml(f) + this._eventsHtml(f) + this._batteryHtml(f) + this._recordingHtml(f) + this._techHtml(f);
    this._wireLinks(f);
    if (loading) $("charts").innerHTML = `<div class="muted">Lade …</div>`;
    else this._renderCharts();
  }

  async _showMap(flightId) {
    await loadCardModule().catch(() => null);
    await customElements.whenDefined("dji-flight-map-card");
    const card = this._map;
    if (!card) return;
    customElements.upgrade(card);
    const config = {
      title: "",
      mode: "flight",
      flight_id: flightId,
      height: 320,
      scan_button: false,
      refresh_seconds: 0,
      spots: false,
      fit: true,
    };
    if (!this._mapReady) {
      card.setConfig(config);
      const mapEl = card.shadowRoot?.getElementById("map");
      const haCard = card.shadowRoot?.querySelector("ha-card");
      if (mapEl && haCard) {
        haCard.style.height = "100%";
        haCard.style.display = "flex";
        haCard.style.flexDirection = "column";
        mapEl.style.height = "auto";
        mapEl.style.flex = "1 1 auto";
        mapEl.style.minHeight = "320px";
      }
      this._mapReady = true;
      card.hass = this._hass;
    } else {
      card.setCursor?.(null, null);
      card.updateOptions({ flight_id: flightId });
    }
  }

  // -- sections ---------------------------------------------------------------

  _tilesHtml(f) {
    const bat =
      f.battery_start_pct != null && f.battery_end_pct != null ? `${f.battery_start_pct} → ${f.battery_end_pct} %` : "–";
    const tiles = [
      ["Dauer", fmtDur(f.duration_s)],
      ["Strecke", fmtDist(f.distance_m)],
      ["Max. Entfernung", fmtDist(f.max_distance_m)],
      ["Max. Höhe", fmtNum(f.max_height_m, 0, "m")],
      ["Max. Speed", fmtNum((f.max_h_speed_ms || 0) * 3.6, 1, "km/h")],
      ["Akku", bat],
      ["Video", f.video_time_s == null ? "–" : fmtClock(f.video_time_s)],
    ];
    return tiles
      .map(([k, v]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`)
      .join("");
  }

  _bannerHtml(f) {
    const msgs = [];
    if (f.incident === "critical" || f.incident === "warning") {
      const text = this._labels?.incidentText ? this._labels.incidentText(f) : (f.incident_actions || []).join(", ");
      msgs.push(
        `<div class="banner${f.incident === "critical" ? " crit" : ""}">${f.incident === "critical" ? "Kritischer Vorfall" : "Warnung"}: ${esc(text)}</div>`,
      );
    }
    if (f.sd_full) msgs.push(`<div class="banner">SD-Karte war voll: danach wurde nichts mehr aufgezeichnet.</div>`);
    if (f.status === "header_only") {
      msgs.push(
        `<div class="banner">Nur Kopfdaten: das Log ist verschlüsselt und es ist kein DJI-API-Key eingetragen. Verlauf, Akku und Flugmodi fehlen daher.</div>`,
      );
    }
    return msgs.join("");
  }

  _modesHtml(f) {
    const entries = Object.entries(f.mode_time_s || {});
    if (!entries.length) return "";
    // Merge modes that share a label (GPS_GENTLE and GPS_ATTI are both "Normal").
    const byLabel = {};
    for (const [mode, s] of entries) {
      const label = this._modeLabel(mode);
      byLabel[label] = (byLabel[label] || 0) + s;
    }
    const total = Object.values(byLabel).reduce((a, b) => a + b, 0) || 1;
    const rows = Object.entries(byLabel)
      .sort((a, b) => b[1] - a[1])
      .map(
        ([label, s]) => `
        <div class="row">
          <div>${esc(label)}</div>
          <div class="bar"><div style="width:${((100 * s) / total).toFixed(1)}%;background:${modeColor(label)}"></div></div>
          <div class="t">${esc(fmtClock(s))}</div>
        </div>`,
      )
      .join("");
    return `<div class="card"><h3>Flugmodi</h3><div class="bars">${rows}</div></div>`;
  }

  _eventsHtml(f) {
    const events = this._track?.events || [];
    if (!this._track) return "";
    const incident = new Set(f.incident_actions || []);
    const cls = (a) => (incident.has(a) ? (f.incident === "critical" ? "crit" : "warn") : "");
    const rows = events.length
      ? events
          .map(
            ([t, a]) =>
              `<div><span class="at">${esc(fmtClock(t))}</span><span class="${cls(a)}">${esc(this._actionLabel(a))}</span></div>`,
          )
          .join("")
      : `<div class="muted">Keine Eingriffe des Flugcontrollers.</div>`;
    return `<div class="card"><h3>Ereignisse</h3><div class="events">${rows}</div></div>`;
  }

  _batteryHtml(f) {
    if (!f.battery_sn && f.battery_cycles == null) return "";
    const cap =
      f.battery_full_mah && f.battery_design_mah
        ? `${f.battery_full_mah} / ${f.battery_design_mah} mAh (${Math.round((100 * f.battery_full_mah) / f.battery_design_mah)} %)`
        : "–";
    const temp =
      f.battery_temp_max_c != null ? `${fmtNum(f.battery_temp_start_c, 1)} → max. ${fmtNum(f.battery_temp_max_c, 1, "°C")}` : "–";
    const hot = f.battery_temp_max_c != null && f.battery_temp_max_c > 60;
    const low = f.battery_cell_min_v != null && f.battery_cell_min_v < 3.0;
    const drift = f.battery_cell_dev_max_v != null && f.battery_cell_dev_max_v > 0.2;
    const rows = [
      ["Seriennummer", f.battery_sn || "–"],
      ["Ladung", f.battery_start_pct != null ? `${f.battery_start_pct} → ${f.battery_end_pct} %` : "–"],
      ["Ladezyklen", f.battery_cycles ?? "–"],
      ["Lebensdauer", f.battery_life_pct != null ? `${f.battery_life_pct} %` : "–"],
      ["Kapazität", cap],
      ["Temperatur", temp, hot ? "warn" : ""],
      ["Min. Zellspannung", fmtNum(f.battery_cell_min_v, 2, "V"), low ? "warn" : ""],
      ["Max. Zellabweichung", fmtNum(f.battery_cell_dev_max_v, 3, "V"), drift ? "warn" : ""],
    ];
    return `<div class="card"><h3>Akku</h3>${table(rows)}</div>`;
  }

  _recordingHtml(f) {
    const sd =
      f.sd_total_mb != null
        ? `${fmtNum(f.sd_free_mb / 1024, 1)} von ${fmtNum(f.sd_total_mb / 1024, 1, "GB")} frei${f.sd_full ? " (voll)" : ""}`
        : "–";
    const rows = [
      ["Video", f.video_time_s == null ? "–" : fmtClock(f.video_time_s)],
      ["Fotos", f.photo_num ?? "–"],
      ["SD-Karte", sd, f.sd_full ? "warn" : ""],
    ];
    return `<div class="card"><h3>Aufnahme</h3>${table(rows)}</div>`;
  }

  _techHtml(f) {
    const rows = [
      ["Drohne", `${f.aircraft_name || "DJI"}${f.aircraft_sn ? ` (${f.aircraft_sn})` : ""}`],
      ["Start", fmtDate(f.start_time, { dateStyle: "medium", timeStyle: "medium" })],
      ["Ende", f.end_time ? fmtDate(f.end_time, { dateStyle: "medium", timeStyle: "medium" }) : "–"],
      ["Max. Sinken/Steigen", fmtNum(f.max_v_speed_ms, 1, "m/s")],
      ["App", f.app_version || "–"],
      ["Log-Version", f.log_version ?? "–"],
      ["Datei", f.filename || "–"],
    ];
    const links = f.points
      ? `<div class="links">${["gpx", "kml", "geojson"].map((x) => `<a data-fmt="${x}">${x.toUpperCase()}</a>`).join("")}</div>`
      : "";
    return `<div class="card"><h3>Technik</h3>${table(rows)}${links}</div>`;
  }

  _wireLinks(f) {
    for (const a of this.shadowRoot.querySelectorAll("a[data-fmt]")) {
      a.onclick = async (ev) => {
        ev.preventDefault();
        try {
          const mod = await loadCardModule();
          await mod.downloadExport(this._hass, f, a.dataset.fmt);
        } catch (err) {
          console.error("dji-flight-details export:", err);
        }
      };
    }
  }

  _modeLabel(mode) {
    return this._labels?.modeLabel ? this._labels.modeLabel(mode) : mode;
  }

  _actionLabel(action) {
    return this._labels?.actionLabel ? this._labels.actionLabel(action) : action;
  }

  // -- charts -----------------------------------------------------------------

  _renderCharts() {
    const el = this.shadowRoot.getElementById("charts");
    if (!el) return;
    const p = this._track?.profile;
    if (!p || !p.t?.length) {
      el.innerHTML = `<div class="muted">Kein Verlauf für diesen Flug${
        this._flight?.status === "header_only" ? " (nur Kopfdaten)" : " (vor dem Update importiert; wird beim nächsten Scan nachgeladen)"
      }.</div>`;
      return;
    }
    const width = el.clientWidth;
    if (!width) {
      // Not laid out yet (hidden tab); the ResizeObserver catches up.
      this._observe(el);
      return;
    }
    this._width = width;
    const t0 = p.t[0];
    const t1 = p.t[p.t.length - 1] || t0 + 1;
    const plotW = Math.max(10, width - PAD_L - PAD_R);
    const x = (t) => PAD_L + ((t - t0) / (t1 - t0 || 1)) * plotW;
    const events = this._track.events || [];
    const incident = new Set(this._flight?.incident_actions || []);
    const eventColor = (a) =>
      incident.has(a) ? (this._flight.incident === "critical" ? "var(--error-color, #db4437)" : "var(--warning-color, #ffa600)") : "var(--secondary-text-color)";
    const eventLines = (h) =>
      events
        .map(
          ([t, a]) =>
            `<line class="event" x1="${x(t)}" x2="${x(t)}" y1="0" y2="${h}" style="stroke:${eventColor(a)}"><title>${esc(
              `${fmtClock(t)} ${this._actionLabel(a)}`,
            )}</title></line>`,
        )
        .join("");

    let html = this._bandHtml(x, width);
    this._charts = [];
    for (const c of CHARTS.filter((def) => (p[def.key] || []).some((v) => v != null))) {
      const scale = c.scale || 1;
      const vals = p[c.key].map((v) => (v == null ? null : v * scale));
      const known = vals.filter((v) => v != null);
      let lo = c.range ? c.range[0] : Math.min(...known);
      let hi = c.range ? c.range[1] : Math.max(...known);
      if (c.floor != null) lo = Math.min(lo, c.floor);
      if (hi - lo < 1) hi = lo + 1;
      const pad = c.range ? 0 : (hi - lo) * 0.05;
      lo = c.floor != null && lo === c.floor ? lo : lo - pad;
      hi += pad;
      const y = (v) => 4 + (1 - (v - lo) / (hi - lo)) * (CHART_H - 8);
      let d = "";
      let pen = false;
      vals.forEach((v, i) => {
        if (v == null) {
          pen = false;
          return;
        }
        d += `${pen ? "L" : "M"}${x(p.t[i]).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      });
      const peak = Math.max(...known);
      html += `
        <div class="chart" data-key="${c.key}">
          <div class="lbl"><span>${esc(c.label)}</span><span class="val">max. <b>${esc(fmtNum(peak, c.digits, c.unit))}</b></span></div>
          <svg width="${width}" height="${CHART_H}">
            <line class="grid" x1="${PAD_L}" x2="${width - PAD_R}" y1="${y(hi)}" y2="${y(hi)}"></line>
            <line class="grid" x1="${PAD_L}" x2="${width - PAD_R}" y1="${y(lo)}" y2="${y(lo)}"></line>
            <text x="${PAD_L - 6}" y="${y(hi) + 3}" text-anchor="end">${esc(fmtNum(hi, hi - lo < 10 ? 1 : 0))}</text>
            <text x="${PAD_L - 6}" y="${y(lo) + 3}" text-anchor="end">${esc(fmtNum(lo, hi - lo < 10 ? 1 : 0))}</text>
            ${eventLines(CHART_H)}
            <path d="${d}" fill="none" stroke="${c.color}" stroke-width="1.8" stroke-linejoin="round"></path>
            <line class="cursor" x1="0" x2="0" y1="0" y2="${CHART_H}" visibility="hidden"></line>
            <circle r="3.5" fill="${c.color}" visibility="hidden"></circle>
          </svg>
        </div>`;
      this._charts.push({ ...c, y, vals });
    }
    html += this._axisHtml(x, t0, t1, width);
    el.innerHTML = `<div class="charts">${html}</div>`;
    this._wireHover(el.querySelector(".charts"), p, x, t0, t1, plotW);
    this._observe(el);
  }

  _bandHtml(x, width) {
    const modes = this._track?.modes || [];
    if (!modes.length) return "";
    const rects = modes
      .map(([a, b, m]) => {
        const label = this._modeLabel(m);
        return `<rect x="${x(a)}" y="0" width="${Math.max(1, x(b) - x(a))}" height="12" fill="${modeColor(label)}"><title>${esc(
          `${label}: ${fmtClock(a)}–${fmtClock(b)}`,
        )}</title></rect>`;
      })
      .join("");
    const seen = [...new Set(modes.map(([, , m]) => this._modeLabel(m)))];
    return `
      <div class="band">
        <svg width="${width}" height="12">${rects}</svg>
      </div>
      <div class="legend">${seen.map((l) => `<span><i style="background:${modeColor(l)}"></i>${esc(l)}</span>`).join("")}</div>`;
  }

  _axisHtml(x, t0, t1, width) {
    const span = t1 - t0;
    const step = [10, 15, 30, 60, 120, 300, 600, 900, 1800].find((s) => span / s <= 8) || 3600;
    let ticks = "";
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      ticks += `<text x="${x(t)}" y="10" text-anchor="middle">${esc(fmtClock(t))}</text>`;
    }
    return `<svg width="${width}" height="14">${ticks}</svg>`;
  }

  _wireHover(root, p, x, t0, t1, plotW) {
    const svgs = [...root.querySelectorAll(".chart svg")];
    const labels = [...root.querySelectorAll(".chart .val")];
    const peaks = labels.map((l) => l.innerHTML);
    const show = (i) => {
      const cx = x(p.t[i]);
      this._charts.forEach((c, k) => {
        const svg = svgs[k];
        const line = svg.querySelector(".cursor");
        const dot = svg.querySelector("circle");
        line.setAttribute("x1", cx);
        line.setAttribute("x2", cx);
        line.setAttribute("visibility", "visible");
        const v = c.vals[i];
        if (v == null) {
          dot.setAttribute("visibility", "hidden");
          labels[k].innerHTML = `${esc(fmtClock(p.t[i]))} · –`;
        } else {
          dot.setAttribute("cx", cx);
          dot.setAttribute("cy", c.y(v));
          dot.setAttribute("visibility", "visible");
          labels[k].innerHTML = `${esc(fmtClock(p.t[i]))} · <b>${esc(fmtNum(v, c.digits, c.unit))}</b>`;
        }
      });
      this._map?.setCursor?.(p.lat?.[i], p.lon?.[i]);
    };
    const hide = () => {
      svgs.forEach((svg) => {
        svg.querySelector(".cursor").setAttribute("visibility", "hidden");
        svg.querySelector("circle").setAttribute("visibility", "hidden");
      });
      labels.forEach((l, k) => (l.innerHTML = peaks[k]));
      this._map?.setCursor?.(null, null);
    };
    root.onpointermove = (e) => {
      const rect = root.getBoundingClientRect();
      const px = e.clientX - rect.left;
      if (px < PAD_L - 4 || px > PAD_L + plotW + 4) return hide();
      const t = t0 + ((px - PAD_L) / plotW) * (t1 - t0);
      show(nearest(p.t, t));
    };
    root.onpointerleave = hide;
  }

  _observe(el) {
    if (this._ro) return;
    this._ro = new ResizeObserver(() => {
      const target = this.shadowRoot.getElementById("charts");
      if (target && target.clientWidth && Math.abs(target.clientWidth - this._width) > 2) this._renderCharts();
    });
    this._ro.observe(this);
  }
}

function table(rows) {
  return `<table>${rows
    .map(([k, v, cls = ""]) => `<tr><td>${esc(k)}</td><td class="${cls}">${esc(v)}</td></tr>`)
    .join("")}</table>`;
}

function modeColor(label) {
  return MODE_COLORS[label] || OTHER_MODE_COLOR;
}

/** Index of the sample closest to t in the ascending array ts. */
function nearest(ts, t) {
  let lo = 0;
  let hi = ts.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] < t) lo = mid;
    else hi = mid;
  }
  return Math.abs(ts[lo] - t) <= Math.abs(ts[hi] - t) ? lo : hi;
}

if (!customElements.get("dji-flight-details")) {
  customElements.define("dji-flight-details", DjiFlightDetails);
}
