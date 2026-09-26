/*
 * dji-flight-details
 *
 * Detail view of one flight for the dji_flightlog panel: key figures, the
 * track on a map, charts over the flight time (height, speed, distance from
 * home, battery, battery temperature) with the flight modes and flight
 * controller events, and the battery / recording / technical data.
 * With a media source (OneDrive, folder) connected, the recordings of the flight show in a
 * player that can be enlarged to fill the window. A playing video moves
 * the cursor in the charts and on the map along; a click into the charts
 * seeks the video to that moment. 360° recordings play in a 360° view
 * (dji-360-view: drag to look around, zoom; "Flach" shows the raw picture).
 *
 * On wide screens video, map and charts share one area that fills the rest
 * of the screen: video over map on the left, charts on the right. The
 * dividers can be dragged (double click resets); the sizes are kept per
 * browser. Narrow screens stack everything.
 *
 * The panel creates it, sets `hass`, hands over the (filtered) flight list
 * with `setFlights()` and picks one with `show(flightId)`. Prev/next inside
 * the view fire `dji-flight-selected` so the panel can follow.
 * With pilots set up (`setPilots()`), the head has a pilot picker; a change is
 * saved right away and fires `dji-flight-pilot`.
 * The note on a flight saves itself while typing (after a pause and when the
 * field loses focus) and fires `dji-flight-note`.
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
// The 360° player, loaded once a flight with videos is shown; it also decides
// which recordings are 360° (view360.projectionOf), known here once loaded.
let viewModule = null;
let view360 = null;
const load360 = () =>
  (viewModule ??= import(`${STATIC}/dji-360-view.js${VERSION_QUERY}`).then((mod) => (view360 = mod)));

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

/** Link to a recording at its source: OneDrive's web view, or the original as a download (local folder). */
const sourceLink = (m, short = false) =>
  m.web_url
    ? `<a href="${esc(m.web_url)}" target="_blank" rel="noopener">${short ? "OneDrive" : "In OneDrive öffnen"}</a>`
    : m.download
      ? `<a href="${esc(m.download)}" download>${short ? "Download" : "Original herunterladen"}</a>`
      : "";
/** Where to look at a recording the browser cannot show. */
const sourceHint = (m) =>
  m.web_url ? "Über „In OneDrive öffnen“ ansehen oder herunterladen." : m.download ? "Das Original lässt sich herunterladen." : "";

const CHARTS = [
  { key: "height", label: "Höhe über Start", short: "Höhe", unit: "m", color: "#4363d8", digits: 0, floor: 0 },
  { key: "speed", label: "Geschwindigkeit", short: "Speed", unit: "km/h", color: "#f58231", digits: 0, scale: 3.6, floor: 0 },
  { key: "dist", label: "Entfernung vom Home-Punkt", short: "Entfernung", unit: "m", color: "#3cb44b", digits: 0, floor: 0 },
  { key: "battery", label: "Akku", short: "Akku", unit: "%", color: "#e6194b", digits: 0, range: [0, 100] },
  { key: "temp", label: "Akku-Temperatur", short: "Temp.", unit: "°C", color: "#911eb4", digits: 1 },
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

// Manual shift of a recording against the log, in seconds, per recording id.
const ADJUST_KEY = "dji_flightlog.media_shift.";
// A file name's time within this of a recording start in the log snaps to it.
const SNAP_S = 5;

// Divider positions of the flight view, per browser.
const LAYOUT_KEY = "dji_flightlog.detail_layout";
const LAYOUT_DEFAULTS = { lr: 0.45, v: 0.55 }; // left column share, video share of it
const MIN_WORK_H = 460;
const MAX_WORK_H = 2000;

const PAD_L = 44;
const PAD_R = 10;
const CHART_H = 86; // stacked layout; the split layout fits the charts to their pane
const MIN_CHART_H = 44;
const MAX_CHART_H = 220;

class DjiFlightDetails extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._flights = [];
    this._pilots = [];
    this._noteFor = null; // flight whose note the field shows
    this._noteTimer = null;
    this._id = null;
    this._flight = null;
    this._track = null;
    this._labels = null;
    this._seq = 0;
    this._width = 0;
    this._hover = null;
    this._chartBox = { w: 0, h: 0 };
    this._durations = {}; // recording id -> seconds, read from the video where the source knows none
    this._sizes = {}; // recording id -> { width, height } of the video, once read (tells a 2:1 fisheye proxy)
    this._probed = new Set(); // recording ids whose metadata were fetched or are queued
    this._prefs = readLayout();
    this._onResize = () => this._layout();
    this._render();
  }

  connectedCallback() {
    window.addEventListener("resize", this._onResize);
    this._layout();
  }

  disconnectedCallback() {
    window.removeEventListener("resize", this._onResize);
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
    // New or removed recordings change the band in the charts.
    if (this._renderMedia() && this._track) this._renderCharts();
  }

  /** The pilots to pick from in the head; none: no picker. */
  setPilots(pilots) {
    this._pilots = pilots || [];
    this._renderHead();
  }

  /** Stop playback and leave the enlarged player (the panel calls this when the view is left). */
  pause() {
    this._setBig(false);
    this.shadowRoot.querySelector("#media video")?.pause();
  }

  async show(flightId) {
    if (!this._hass) {
      this._pendingId = flightId;
      return;
    }
    this._pendingId = null;
    this._flushNote();
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
    this._probeDurations();
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
        .head .pilot { display: flex; align-items: center; gap: 6px; margin-top: 4px; font-size: 13px; color: var(--secondary-text-color); }
        .head .pilot select {
          font: inherit; font-size: 13px; padding: 3px 6px; border-radius: 6px; max-width: 100%;
          background: var(--card-background-color, #fff); color: var(--primary-text-color);
          border: 1px solid var(--divider-color, #e0e0e0);
        }
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
        .card { padding: 12px 14px; min-width: 0; }
        .card h3 { margin: 0 0 8px; font-size: 14px; font-weight: 500; }
        .mapcard { padding: 0; overflow: hidden; display: flex; isolation: isolate; }
        .mapcard dji-flight-map-card { flex: 1; display: block; min-width: 0; }

        /* split layout: video over map | charts */
        .work { display: flex; height: 600px; min-height: 0; }
        .left { flex: 0 0 calc(var(--lr) * 100%); min-width: 0; display: flex; flex-direction: column; }
        #media { flex: 0 0 calc(var(--v) * 100%); min-height: 120px; box-sizing: border-box; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
        #media[hidden] { display: none; }
        .left .mapcard { flex: 1 1 0; min-height: 120px; }
        .chartcard { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; overflow-y: auto; }
        .chartcard #charts { flex: 1 1 0; min-height: 0; }
        .split { flex: 0 0 12px; display: flex; align-items: center; justify-content: center; touch-action: none; outline: none; }
        .split[hidden] { display: none; }
        .split::after { content: ""; border-radius: 2px; background: var(--divider-color, #d0d0d0); transition: background .15s; }
        .split.col { cursor: col-resize; }
        .split.col::after { width: 4px; height: 40px; }
        .split.row { cursor: row-resize; }
        .split.row::after { width: 40px; height: 4px; }
        .split:hover::after, .split:focus-visible::after, .split.drag::after { background: var(--primary-color); }
        :host(.narrow) .work { flex-direction: column; height: auto !important; gap: 12px; }
        :host(.narrow) .left { flex: none; gap: 12px; }
        :host(.narrow) #media { flex: none; }
        :host(.narrow) .left .mapcard { flex: none; height: 320px; }
        :host(.narrow) .chartcard { flex: none; overflow: visible; }
        :host(.narrow) .chartcard #charts { flex: none; }
        :host(.narrow) .split { display: none; }
        .info { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        td { padding: 3px 0; vertical-align: top; }
        td:first-child { color: var(--secondary-text-color); padding-right: 12px; white-space: nowrap; }
        td.warn { color: var(--warning-color, #ffa600); }
        td.crit { color: var(--error-color, #db4437); }
        .muted { color: var(--secondary-text-color); font-size: 13px; }
        #notecard:empty { display: none; }
        #notecard .nh { display: flex; align-items: baseline; gap: 8px; }
        #notecard .nh h3 { flex: 1; }
        #notecard .st { font-size: 12px; color: var(--secondary-text-color); }
        #notecard .st.err { color: var(--error-color, #db4437); }
        #notecard textarea {
          display: block; width: 100%; box-sizing: border-box; min-height: 60px; resize: vertical;
          font: inherit; font-size: 14px; line-height: 1.4; padding: 8px 10px; border-radius: 8px;
          background: var(--card-background-color, #fff); color: inherit;
          border: 1px solid var(--divider-color, #e0e0e0);
        }
        #notecard textarea:focus { outline: none; border-color: var(--primary-color); }
        button.addnote {
          align-self: flex-start; font: inherit; font-size: 13px; cursor: pointer; padding: 4px 10px;
          border-radius: 6px; border: 1px dashed var(--divider-color, #e0e0e0);
          background: none; color: var(--primary-color);
        }
        button.addnote:hover { background: var(--secondary-background-color, #f2f2f2); }
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
        .recband [data-rec] { fill: var(--primary-color); opacity: 0.4; cursor: pointer; }
        .recband circle[data-rec] { stroke: var(--card-background-color, #fff); stroke-width: 1; }
        .recband [data-rec].sel { opacity: 1; }
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

        /* recordings */
        .stage { flex: 1 1 0; min-height: 0; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
        .screen { position: relative; flex: 1 1 0; min-height: 60px; border-radius: 8px; overflow: hidden; background: #000; }
        :host(.narrow) .stage { flex: none; }
        :host(.narrow) .screen { flex: none; aspect-ratio: 16 / 9; }
        .screen video, .screen img { width: 100%; height: 100%; object-fit: contain; display: block; background: #000; }
        .screen .ph { height: 100%; display: flex; align-items: center; justify-content: center; color: #bbb; font-size: 13px; }
        .screen .grow {
          position: absolute; top: 6px; right: 6px; z-index: 1; width: 32px; height: 32px; padding: 4px;
          border: none; border-radius: 50%; background: rgba(0,0,0,.55); color: #fff; cursor: pointer; line-height: 0;
        }
        .screen .grow:hover { background: rgba(0,0,0,.8); }
        .screen.busy::after {
          content: "Lade Video …"; position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
          background: rgba(0,0,0,.45); color: #fff; font-size: 13px; pointer-events: none;
        }
        .screen .grow svg { width: 24px; height: 24px; }
        .stage .cap { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--secondary-text-color); }
        .stage .cap a { color: var(--primary-color); text-decoration: none; white-space: nowrap; }
        .stage .muted:empty { display: none; }
        .stage.big {
          position: fixed; inset: 0; z-index: 10; padding: 16px; box-sizing: border-box;
          background: rgba(0,0,0,.88); align-items: center; justify-content: center;
        }
        .stage.big .screen { width: min(1400px, 100%); aspect-ratio: auto; height: calc(100dvh - 90px); border-radius: 0; }
        .stage.big .cap, .stage.big .muted { width: min(1400px, 100%); color: #ddd; }
        .stage.big .cap a { color: #8ab4f8; }
        .stage .sync button {
          width: 22px; height: 20px; margin: 0 4px; padding: 0; line-height: 1; cursor: pointer;
          border: 1px solid var(--divider-color, #ccc); border-radius: 4px; background: none; color: inherit;
        }
        .stage .hud { display: none; }
        .stage.big.synced .screen { height: calc(100dvh - 170px); }
        .stage.big.synced .hud { display: flex; flex-direction: column; gap: 4px; width: min(1400px, 100%); font-size: 13px; color: #ddd; }
        .hud svg { width: 100%; height: 40px; cursor: pointer; }
        .hud .range { fill: rgba(255,255,255,.14); }
        .hud path { fill: none; stroke: #8ab4f8; stroke-width: 1.5; }
        .hud .cur { stroke: #fff; stroke-width: 1.5; }
        .strip { flex: none; display: flex; gap: 6px; overflow-x: auto; padding: 2px; }
        .strip .m {
          position: relative; flex: none; width: 80px; height: 45px; border-radius: 6px; overflow: hidden; cursor: pointer;
          background: var(--divider-color, #ddd); display: flex; align-items: center; justify-content: center;
          font-size: 11px; color: var(--secondary-text-color); outline-offset: 1px;
        }
        .strip .m img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
        .strip .m span {
          position: absolute; right: 3px; bottom: 3px; font-size: 10px; line-height: 1; padding: 2px 4px;
          border-radius: 3px; background: rgba(0,0,0,.6); color: #fff;
        }
        .strip .m:hover { outline: 2px solid var(--divider-color, #bbb); }
        .strip .m.sel { outline: 2px solid var(--primary-color); }
      </style>
      <div class="wrap">
        <div class="head" id="head"></div>
        <div class="empty" id="empty">Noch kein Flug ausgewählt.<br>In der Liste unter „Flüge“ einen Flug antippen.</div>
        <div class="content" id="content" hidden>
          <div class="tiles" id="tiles"></div>
          <div id="banner"></div>
          <div id="notecard"></div>
          <div>
            <div class="work" id="work">
              <div class="left" id="left">
                <div class="card" id="media" hidden></div>
                <div class="split row" id="split-v" data-key="v" role="separator" aria-orientation="horizontal" tabindex="0" title="Ziehen ändert die Größe von Video und Karte, Doppelklick setzt zurück" hidden></div>
                <!-- Built once: re-inserting the map would detach Leaflet and its tile token refresh. -->
                <div class="card mapcard"><dji-flight-map-card></dji-flight-map-card></div>
              </div>
              <div class="split col" id="split-lr" data-key="lr" role="separator" aria-orientation="vertical" tabindex="0" title="Ziehen ändert die Breite, Doppelklick setzt zurück"></div>
              <div class="card chartcard">
                <h3>Verlauf</h3>
                <div id="charts"></div>
              </div>
            </div>
            <div class="split row grip" id="split-h" data-key="h" role="separator" aria-orientation="horizontal" tabindex="0" title="Ziehen ändert die Höhe, Doppelklick passt sie wieder an den Bildschirm an"></div>
          </div>
          <div class="info" id="info"></div>
        </div>
      </div>`;
    this._wireSplitters();
    this._applyLayout();
  }

  // -- split layout -------------------------------------------------------------

  /** Size the split area: the rest of the screen below the key figures, or the height dragged to. */
  _layout() {
    const work = this.shadowRoot.getElementById("work");
    this._applyLayout();
    if (!work || !this.isConnected || this.classList.contains("narrow") || !work.offsetParent) return;
    let h = this._prefs.h;
    if (!h) {
      const scroller = scrollParent(this);
      const viewTop = scroller ? scroller.getBoundingClientRect().top : 0;
      const viewH = scroller ? scroller.clientHeight : window.innerHeight;
      const top = work.getBoundingClientRect().top - viewTop + (scroller ? scroller.scrollTop : window.scrollY);
      // Below the key figures if there is room, else the whole screen once scrolled down to it.
      h = viewH - top - 16;
      if (h < MIN_WORK_H) h = viewH - 16;
    }
    h = `${Math.round(Math.min(MAX_WORK_H, Math.max(MIN_WORK_H, h)))}px`;
    if (work.style.height !== h) work.style.height = h;
  }

  _applyLayout() {
    const work = this.shadowRoot.getElementById("work");
    if (!work) return;
    const { lr, v } = { ...LAYOUT_DEFAULTS, ...this._prefs };
    work.style.setProperty("--lr", String(lr));
    work.style.setProperty("--v", String(v));
  }

  _wireSplitters() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const work = $("work");
    const left = $("left");
    const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
    const set = (key, value) => {
      if (value == null) delete this._prefs[key];
      else this._prefs[key] = key === "h" ? Math.round(value) : Math.round(value * 1000) / 1000;
      this._layout();
    };
    // Position of the divider from the pointer, per divider.
    const fromPointer = {
      lr: (e) => {
        const r = work.getBoundingClientRect();
        return clamp((e.clientX - r.left) / r.width, 0.2, 0.75);
      },
      v: (e) => {
        const r = left.getBoundingClientRect();
        return clamp((e.clientY - r.top) / r.height, 0.15, 0.85);
      },
      h: (e) => clamp(e.clientY - work.getBoundingClientRect().top - 6, MIN_WORK_H, MAX_WORK_H),
    };
    const current = (key) => (key === "h" ? this._prefs.h || work.offsetHeight : this._prefs[key] ?? LAYOUT_DEFAULTS[key]);
    for (const el of this.shadowRoot.querySelectorAll(".split")) {
      const key = el.dataset.key;
      el.onpointerdown = (e) => {
        if (e.button) return;
        e.preventDefault();
        el.setPointerCapture(e.pointerId);
        el.classList.add("drag");
        el.onpointermove = (ev) => set(key, fromPointer[key](ev));
        el.onpointerup = el.onpointercancel = () => {
          el.onpointermove = el.onpointerup = el.onpointercancel = null;
          el.classList.remove("drag");
          writeLayout(this._prefs);
        };
      };
      el.ondblclick = () => {
        set(key, null);
        writeLayout(this._prefs);
      };
      el.onkeydown = (e) => {
        const back = e.key === "ArrowLeft" || e.key === "ArrowUp";
        if (!back && e.key !== "ArrowRight" && e.key !== "ArrowDown") return;
        e.preventDefault();
        const step = key === "h" ? 40 : 0.02;
        const [lo, hi] = key === "h" ? [MIN_WORK_H, MAX_WORK_H] : key === "lr" ? [0.2, 0.75] : [0.15, 0.85];
        set(key, clamp(current(key) + (back ? -step : step), lo, hi));
        writeLayout(this._prefs);
      };
    }
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
        ${this._pilotHtml(f)}
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
    const pick = head.querySelector("#pilot");
    if (pick) pick.onchange = () => this._assignPilot(f, pick.value);
  }

  /**
   * Note field. Rebuilt only for another flight, so reloads of the flight list
   * never touch what is being typed. Without a note: just an "add" button.
   */
  _renderNote(f, { edit = false, force = false } = {}) {
    const box = this.shadowRoot.getElementById("notecard");
    if (!edit && !force && f.flight_id === this._noteFor && box.firstElementChild) return;
    this._noteFor = f.flight_id;
    this._noteSaved = f.note || "";
    box.className = "";
    if (!this._noteSaved && !edit) {
      box.innerHTML = `<button class="addnote">+ Notiz</button>`;
      box.firstElementChild.onclick = () => this._renderNote(f, { edit: true });
      return;
    }
    box.className = "card";
    box.innerHTML = `
      <div class="nh"><h3>Notiz</h3><span class="st" id="notest"></span></div>
      <textarea id="note" maxlength="2000" placeholder="z. B. Wetter, Wind, wer dabei war, was geübt wurde …"></textarea>`;
    const ta = box.querySelector("textarea");
    ta.value = this._noteSaved;
    const fit = () => {
      ta.style.height = "auto";
      ta.style.height = `${Math.min(ta.scrollHeight + 2, 320)}px`;
    };
    requestAnimationFrame(fit);
    const id = f.flight_id;
    ta.oninput = () => {
      fit();
      this._setNoteStatus("");
      clearTimeout(this._noteTimer);
      this._noteTimer = setTimeout(() => this._saveNote(id, ta.value), 1200);
    };
    ta.onblur = () => {
      this._flushNote();
      // Emptied: back to the small button.
      if (!ta.value.trim() && this._noteFor === id) this._renderNote({ ...f, note: "" }, { force: true });
    };
    if (edit) ta.focus();
  }

  /** Save a note that is still waiting for the typing pause. */
  _flushNote() {
    if (!this._noteTimer) return;
    clearTimeout(this._noteTimer);
    this._noteTimer = null;
    const ta = this.shadowRoot.getElementById("note");
    if (ta && this._noteFor) this._saveNote(this._noteFor, ta.value);
  }

  async _saveNote(flightId, text) {
    this._noteTimer = null;
    const note = text.trim();
    if (flightId === this._noteFor && note === this._noteSaved) return;
    try {
      const res = await this._hass.callApi("PUT", `${API}/flights/${flightId}/note`, { note });
      if (flightId === this._noteFor) {
        this._noteSaved = res.flight?.note ?? note;
        this._setNoteStatus("Gespeichert");
      }
      if (this._flight?.flight_id === flightId) this._flight = { ...this._flight, note: this._noteSaved };
    } catch (err) {
      console.error("dji-flight-details note:", err);
      if (flightId === this._noteFor) this._setNoteStatus("Speichern fehlgeschlagen", true);
      return;
    }
    this.dispatchEvent(new CustomEvent("dji-flight-note", { detail: { flight_id: flightId }, bubbles: true, composed: true }));
  }

  _setNoteStatus(text, error = false) {
    const st = this.shadowRoot.getElementById("notest");
    if (!st) return;
    st.textContent = text;
    st.classList.toggle("err", error);
  }

  /** Pilot picker: automatic (by aircraft), a pilot, or nobody. */
  _pilotHtml(f) {
    if (!this._pilots.length) return "";
    const byAircraft = this._pilots.find((p) => f.aircraft_sn && (p.aircraft || []).includes(f.aircraft_sn));
    const value = f.pilot_source === "manual" ? f.pilot_id || "none" : "auto";
    const opts = [
      ["auto", byAircraft ? `${byAircraft.name} (über die Drohne)` : "Automatisch (keiner)"],
      ...this._pilots.map((p) => [p.id, p.name]),
      ["none", "Kein Pilot"],
    ];
    return `<label class="pilot">Pilot <select id="pilot" title="Wer diesen Flug geflogen ist">${opts
      .map(([v, label]) => `<option value="${esc(v)}"${v === value ? " selected" : ""}>${esc(label)}</option>`)
      .join("")}</select></label>`;
  }

  async _assignPilot(f, value) {
    const pilotId = value === "none" ? null : value;
    try {
      const res = await this._hass.callApi("POST", `${API}/flights/pilot`, { flight_ids: [f.flight_id], pilot_id: pilotId });
      const fresh = res.flights?.[0];
      // The panel may filter this flight out of the list now; keep showing it.
      if (fresh && this._flight?.flight_id === fresh.flight_id) this._flight = { ...this._flight, ...fresh };
    } catch (err) {
      console.error("dji-flight-details pilot:", err);
    }
    this._renderHead();
    this.dispatchEvent(new CustomEvent("dji-flight-pilot", { detail: { flight_id: f.flight_id }, bubbles: true, composed: true }));
  }

  _renderAll({ loading = false } = {}) {
    this._renderHead();
    const $ = (id) => this.shadowRoot.getElementById(id);
    const f = this._flight;
    $("empty").hidden = !!f;
    $("content").hidden = !f;
    this._renderMedia();
    if (!f) return;
    $("tiles").innerHTML = this._tilesHtml(f);
    $("banner").innerHTML = this._bannerHtml(f);
    this._renderNote(f);
    $("info").innerHTML =
      this._modesHtml(f) + this._eventsHtml(f) + this._batteryHtml(f) + this._recordingHtml(f) + this._techHtml(f);
    this._wireLinks(f);
    this._layout();
    if (loading) {
      this._chartCtx = null;
      $("charts").innerHTML = `<div class="muted">Lade …</div>`;
    } else this._renderCharts();
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
        mapEl.style.minHeight = "0";
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
    const media = this._recordings;
    const recorded = f.video_time_s > 0 || f.photo_num > 0;
    const rows = [
      ["Video", f.video_time_s == null ? "–" : fmtClock(f.video_time_s)],
      ["Fotos", f.photo_num ?? "–"],
      ["SD-Karte", sd, f.sd_full ? "warn" : ""],
    ];
    // Only with a media source connected (the summaries then carry a media array).
    if (media && (media.length || recorded)) {
      rows.push(["Dateien", media.length ? `${media.length} verknüpft` : "keine gefunden", media.length ? "" : "warn"]);
    }
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

  // -- recordings (OneDrive, folder) -----------------------------------------

  /** The shown flight's recordings; only the list summaries carry them, not the track response. */
  get _recordings() {
    return this._flights.find((x) => x.flight_id === this._id)?.media;
  }

  /** Rebuild the recordings box if the flight or its recordings changed; true if it did. */
  _renderMedia() {
    const box = this.shadowRoot.getElementById("media");
    const media = this._id ? this._recordings : undefined;
    // Rebuild only when the flight or its recordings change: the summaries are
    // re-sent after every refresh and must not restart a playing video.
    const key = media ? `${this._id}:${media.map((m) => m.id).join(",")}` : "";
    if (key === this._mediaKey) return false;
    this._mediaKey = key;
    this._setBig(false);
    this._drop360();
    this._sync = null;
    this._mediaIndex = null;
    this._photoT = null;
    // No media array: no media source connected. None found: the "Aufnahme" card says so.
    const none = !media?.length;
    box.hidden = none;
    this.shadowRoot.getElementById("split-v").hidden = none;
    if (none) {
      box.innerHTML = "";
      return true;
    }
    box.innerHTML = `
        <div class="stage" id="stage"></div>
        ${
          media.length > 1
            ? `<div class="strip">${media
                .map(
                  (m, i) =>
                    `<div class="m" data-i="${i}" data-p="${this._projection(m) || ""}" title="${esc(m.name)}">${this._thumbHtml(m)}</div>`,
                )
                .join("")}</div>`
            : ""
        }`;
    for (const img of box.querySelectorAll(".strip img")) img.onerror = () => img.remove();
    for (const el of box.querySelectorAll(".strip .m")) el.onclick = () => this._selectMedia(Number(el.dataset.i));
    this._selectMedia(0);
    if (this._track) this._probeDurations();
    // Which videos are 360° is up to the player module; show them as such once it is in.
    if (media.some((m) => m.kind !== "photo" && m.play) && !view360) {
      load360()
        .then(() => {
          if (key !== this._mediaKey) return;
          this._refresh360();
          if (this._track) this._probeDurations(); // proxies whose size tells whether they are 360°
        })
        .catch((err) => console.error("dji-flight-details 360:", err));
    }
    return true;
  }

  /** "equirect", "dfisheye" or null (flat) for a recording, as far as known yet. */
  _projection(m) {
    return view360 ? view360.projectionOf(m, this._sizes[m.id]) : null;
  }

  _kindLabel(m) {
    return this._projection(m) ? KIND_LABEL["360"] : KIND_LABEL[m.kind] || m.kind;
  }

  /** Kind, cover and badge of a recording in the thumbnail strip. */
  _thumbHtml(m) {
    const len = this._recDuration(m);
    return `
      ${esc(this._kindLabel(m))}
      ${m.thumb ? `<img src="${esc(m.thumb)}" loading="lazy" alt="">` : ""}
      <span>${this._projection(m) || m.kind === "360" ? "360° " : ""}${len ? esc(fmtClock(len)) : m.kind === "photo" ? "Foto" : ""}</span>`;
  }

  /**
   * Show what turned out to be 360° as such (player module loaded, a proxy's
   * size read): thumbnails, and the selected recording's hint and 360° view.
   */
  _refresh360() {
    const recs = this._recordings || [];
    for (const el of this.shadowRoot.querySelectorAll(".strip .m")) {
      const m = recs[Number(el.dataset.i)];
      if (!m || el.dataset.p === (this._projection(m) || "")) continue;
      el.dataset.p = this._projection(m) || "";
      el.innerHTML = this._thumbHtml(m);
      const img = el.querySelector("img");
      if (img) img.onerror = () => img.remove();
    }
    const m = recs[this._mediaIndex];
    const stage = this.shadowRoot.getElementById("stage");
    const video = stage?.querySelector("video");
    const projection = m && this._projection(m);
    if (!projection || !video || this._view360?.video === video) return;
    const facts = stage.querySelector("#facts");
    if (facts) facts.textContent = this._facts(m);
    const hint = stage.querySelector("#mhint");
    if (hint && !video.error) hint.textContent = view360.HINT_360[projection] || "";
    this._attach360(video, m, projection);
  }

  /** Length of a recording in seconds: from the media source, the video itself or the log; else null. */
  _recDuration(m) {
    const seg = this._logVideo(m);
    return m?.duration_s || this._durations[m?.id] || (seg?.[1] != null ? seg[1] - seg[0] : null);
  }

  /** Read the length of videos neither the source nor the log knows (OneDrive has none for .LRF). */
  _probeDurations() {
    const key = this._mediaKey;
    const todo = (this._recordings || []).filter(
      (m) =>
        m.kind !== "photo" &&
        m.play &&
        !this._probed.has(m.id) &&
        ((!this._recDuration(m) && !(m.id in this._durations)) || (view360?.needsSize(m) && !this._sizes[m.id])),
    );
    for (const m of todo) this._probed.add(m.id);
    const next = () => {
      const m = todo.shift();
      if (!m) return;
      if (key !== this._mediaKey) {
        // Another flight: the rest is fetched when it is shown again.
        for (const x of [m, ...todo]) this._probed.delete(x.id);
        return;
      }
      // Only the metadata: the browser fetches the header (and the end, where DJI puts it).
      // Held on the element: a detached video nobody references is garbage
      // collected before its metadata arrive (takes ~10 s through OneDrive).
      const v = (this._probe = document.createElement("video"));
      v.preload = "metadata";
      v.muted = true;
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        this._probe = null;
        this._learnSize(m.id, v);
        this._learnDuration(m.id, v.duration);
        v.removeAttribute("src");
        v.load();
        next();
      };
      v.addEventListener("loadedmetadata", done, { once: true });
      v.addEventListener("error", done, { once: true });
      v.src = m.play;
    };
    next();
  }

  /** Remember a video's size: a 2:1 proxy is the dual fisheye and gets the 360° view. */
  _learnSize(id, video) {
    if (this._sizes[id] || !video.videoWidth || !video.videoHeight) return;
    this._sizes[id] = { width: video.videoWidth, height: video.videoHeight };
    const m = (this._recordings || []).find((x) => x.id === id);
    if (m && this._projection(m)) this._refresh360();
  }

  _learnDuration(id, seconds) {
    if (this._durations[id]) return;
    const known = Number.isFinite(seconds) && seconds > 0;
    this._durations[id] = known ? seconds : null; // null: tried, don't again
    if (!known) return;
    const i = (this._recordings || []).findIndex((m) => m.id === id);
    if (i < 0) return;
    const m = this._recordings[i];
    const label = this.shadowRoot.querySelector(`.strip .m[data-i="${i}"] span`);
    if (label) label.textContent = `${this._projection(m) || m.kind === "360" ? "360° " : ""}${fmtClock(seconds)}`;
    this._view360?.update(); // its time display takes the length from here before the video has it
    if (this._track) this._renderCharts(); // bar instead of a dot in the band
  }

  _selectMedia(i) {
    const m = this._recordings?.[i];
    const stage = this.shadowRoot.getElementById("stage");
    if (!m || !stage) return;
    this._mediaIndex = i;
    this._sync = null;
    this._drop360();
    // A photo pins the cursor to where it was taken.
    this._photoT = m.kind === "photo" ? this._recOffset(m) : null;
    this._markSelected();
    const isVideo = m.kind !== "photo" && !!m.play;
    const big = stage.classList.contains("big");
    let screen;
    if (isVideo) {
      // preload="none": nothing is fetched until play is pressed. crossorigin:
      // OneDrive redirects to another host, and WebGL (360° view) may only
      // read the frames if that host allows it (it sends CORS headers).
      screen = `<video controls playsinline preload="none" crossorigin="anonymous"${m.thumb ? ` poster="${esc(m.thumb)}"` : ""} src="${esc(m.play)}"></video>`;
    } else if (m.thumb || m.play) {
      // Photos: the small cover while small, the full picture once enlarged.
      screen = `<img src="${esc((big && m.play) || m.thumb || m.play)}" alt="">`;
    } else {
      screen = `<div class="ph">${esc(KIND_LABEL[m.kind] || m.kind)}</div>`;
    }
    const projection = isVideo ? this._projection(m) : null;
    let hint = "";
    if (projection) hint = view360.HINT_360[projection] || "";
    else if (!m.play) hint = `Im Browser nicht darstellbar (360°-Original ohne Proxy oder RAW). ${sourceHint(m)}`;
    stage.innerHTML = `
      <div class="screen">
        ${screen}
        ${isVideo || m.thumb || m.play ? `<button class="grow" title="${big ? "Verkleinern" : "Vergrößern"}">${svg(big ? ICON_SHRINK : ICON_EXPAND)}</button>` : ""}
      </div>
      <div class="cap">
        <span id="facts">${esc(this._facts(m))}</span>
        ${sourceLink(m)}
      </div>
      <div class="muted" id="mhint">${esc(hint)}</div>
      <div class="muted sync" id="msync"></div>
      <div class="hud" id="hud"></div>`;
    const grow = stage.querySelector(".grow");
    if (grow) grow.onclick = () => this._setBig(!stage.classList.contains("big"));
    stage.onclick = (e) => {
      if (e.target === stage) this._setBig(false); // backdrop of the enlarged player
    };
    const img = stage.querySelector(".screen img");
    if (img) img.onerror = () => img.replaceWith(Object.assign(document.createElement("div"), { className: "ph", textContent: KIND_LABEL[m.kind] || m.kind }));
    const video = stage.querySelector("video");
    if (video) {
      video.onerror = () => {
        stage.querySelector("#mhint").textContent =
          `Dieses Video kann der Browser nicht abspielen (vermutlich H.265/HEVC ohne Hardware-Decoder). ${sourceHint(m)}`;
      };
      if (this._recOffset(m) != null) this._wireVideo(video, m);
      // The size decides for proxies the source could not classify (360° or flat).
      video.addEventListener("loadedmetadata", () => this._learnSize(m.id, video));
      if (projection) this._attach360(video, m, projection);
    }
    this._updateSyncUi();
    this._refreshCursor();
  }

  /**
   * Lay the 360° view over the video (drag to look around, own controls; the
   * video underneath keeps playing and drives the sync), also over one that
   * already plays. Without WebGL the flat video stays.
   */
  _attach360(video, m, projection) {
    if (!view360 || !video.isConnected || this._view360?.video === video) return;
    this._drop360();
    const view = view360.Dji360View.create(video, video.closest(".screen"), { projection, duration: () => this._recDuration(m) });
    if (view) this._view360 = view;
    else {
      const hint = this.shadowRoot.querySelector("#mhint");
      if (hint) hint.textContent = `${hint.textContent} ${view360.HINT_NO_WEBGL}`.trim();
    }
  }

  /** Free the 360° view (its WebGL context) before the player is rebuilt. */
  _drop360() {
    this._view360?.destroy();
    this._view360 = null;
  }

  /** Highlight the selected recording in the thumbnail strip and in the band of the charts. */
  _markSelected() {
    const i = this._mediaIndex;
    for (const el of this.shadowRoot.querySelectorAll(".strip .m")) el.classList.toggle("sel", Number(el.dataset.i) === i);
    for (const el of this.shadowRoot.querySelectorAll(".recband [data-rec]")) el.classList.toggle("sel", Number(el.dataset.rec) === i);
  }

  _facts(m) {
    const offset = this._recOffset(m);
    return [
      this._kindLabel(m),
      m.start ? fmtDate(m.start, { timeStyle: "short" }) : null,
      offset != null && offset >= 0 ? `bei ${fmtClock(offset)} im Flug` : null,
      this._recDuration(m) ? fmtClock(this._recDuration(m)) : null,
      m.has_raw ? "RAW" : null,
    ]
      .filter(Boolean)
      .join(" · ");
  }

  /** Flight second of the recording's start by its file name (camera clock), or null. */
  _fileOffset(m) {
    const start = this._flight?.start_time;
    if (!m?.start || !start) return null;
    return (Date.parse(m.start) - Date.parse(start)) / 1000;
  }

  /**
   * The recording in the log (`[t_start, t_end]`) this video is, if any. The
   * file name's time runs about 2 s ahead of the camera actually recording.
   */
  _logVideo(m) {
    const f = m && m.kind !== "photo" ? this._fileOffset(m) : null;
    if (f == null) return null;
    let best = null;
    for (const seg of this._track?.videos || []) {
      if (Math.abs(seg[0] - f) <= SNAP_S && (!best || Math.abs(seg[0] - f) < Math.abs(best[0] - f))) best = seg;
    }
    return best;
  }

  /** Flight second at which a recording starts (aligned to the log, plus the manual shift), or null. */
  _recOffset(m) {
    const f = this._fileOffset(m);
    if (f == null) return null;
    return (this._logVideo(m)?.[0] ?? f) + readAdjust(m.id);
  }

  /** Follow the video's position with the cursor in the charts and on the map. */
  _wireVideo(video, m) {
    const self = this;
    const s = {
      video,
      id: m.id,
      get offset() {
        return self._recOffset(m);
      },
      get duration() {
        return self._recDuration(m);
      },
      active: false,
      looping: false,
    };
    this._sync = s;
    const tick = () => {
      s.looping = false;
      if (this._sync !== s || !video.isConnected) return;
      this._refreshCursor();
      if (!video.paused) loop();
    };
    // Once per shown frame; without requestVideoFrameCallback (older Firefox) once per repaint.
    const loop = () => {
      if (s.looping) return;
      s.looping = true;
      if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(tick);
      else requestAnimationFrame(tick);
    };
    const refresh = () => {
      if (this._sync === s) this._refreshCursor();
    };
    video.addEventListener("play", () => {
      s.active = true;
      loop();
    });
    video.addEventListener("seeking", () => {
      s.active = true;
      refresh();
    });
    video.addEventListener("seeked", refresh);
    video.addEventListener("timeupdate", refresh);
    video.addEventListener("loadedmetadata", () => this._learnDuration(m.id, video.duration));
  }

  /** Flight second shown in the video, or null before it was started. */
  _videoTime() {
    const s = this._sync;
    if (!s?.active || !s.video.isConnected) return null;
    return s.offset + s.video.currentTime;
  }

  /** Jump the video to flight second t: in the recording picked (`pick`), else the shown one or any that covers t. */
  _seekTo(t, pick = null) {
    const recs = this._recordings || [];
    // Start of recording i if it runs at t; a length not known yet counts as open-ended.
    const covers = (i) => {
      const m = recs[i];
      const a = m && m.kind !== "photo" && m.play ? this._recOffset(m) : null;
      const len = this._recDuration(m);
      return a != null && t >= a && (!len || t <= a + len) ? a : null;
    };
    if (pick != null && covers(pick) == null) {
      if (pick !== this._mediaIndex) this._selectMedia(pick); // a photo
      return;
    }
    let i = pick ?? (covers(this._mediaIndex) != null ? this._mediaIndex : -1);
    if (i < 0) {
      recs.forEach((_, k) => {
        const a = covers(k);
        if (a != null && (i < 0 || a > covers(i))) i = k;
      });
    }
    if (i < 0) return;
    if (i !== this._mediaIndex) this._selectMedia(i);
    const s = this._sync;
    if (!s) return;
    const v = s.video;
    const pos = Math.max(0, t - s.offset);
    s.active = true;
    if (v.readyState >= 1) {
      v.currentTime = pos;
    } else {
      // preload="none": fetch the metadata first, then seek (shows that frame).
      // Through OneDrive that takes a few seconds: DJI puts the index at the end.
      // Clicks while it loads only move the target; the last one wins.
      s.pendingPos = pos;
      if (s.loading) return;
      s.loading = true;
      const screen = v.closest(".screen");
      screen?.classList.add("busy");
      const ready = () => {
        if (!s.loading) return;
        s.loading = false;
        screen?.classList.remove("busy");
        if (v.readyState >= 1) v.currentTime = s.pendingPos;
      };
      v.addEventListener("loadedmetadata", ready, { once: true });
      v.addEventListener("error", ready, { once: true });
      v.preload = "metadata";
      v.load();
    }
  }

  /** Shift the shown recording against the log by d seconds (camera clock vs. log time). */
  _adjust(d) {
    const s = this._sync;
    if (!s) return;
    const value = Math.round((readAdjust(s.id) + d) * 10) / 10;
    try {
      if (value) localStorage.setItem(ADJUST_KEY + s.id, String(value));
      else localStorage.removeItem(ADJUST_KEY + s.id);
    } catch {
      // No storage (private window): nothing to keep it in.
    }
    this._renderCharts(); // moves the recording's bar, redraws the sync line
  }

  /** Sync line under the player and, for the enlarged player, values and a mini height curve. */
  _updateSyncUi() {
    const stage = this.shadowRoot.getElementById("stage");
    const line = stage?.querySelector("#msync");
    const hud = stage?.querySelector("#hud");
    if (!line || !hud) return;
    const s = this._sync;
    const ctx = this._chartCtx;
    const on = !!(s && ctx);
    stage.classList.toggle("synced", on);
    if (!on) {
      line.innerHTML = "";
      hud.innerHTML = "";
      return;
    }
    const m = this._recordings?.[this._mediaIndex];
    const facts = stage.querySelector("#facts");
    if (m && facts) facts.textContent = this._facts(m);
    const aligned = m && this._logVideo(m);
    const adj = readAdjust(s.id);
    line.innerHTML = `Läuft mit Verlauf und Karte mit${
      aligned
        ? ` · <span title="Zeit aus dem Dateinamen: ${esc(fmtClock(this._fileOffset(m)))}; die Kamera nimmt erst etwas später auf">am Aufnahmestart im Log ausgerichtet</span>`
        : ""
    } · Versatz
      <button data-d="-1" title="Video 1 s früher im Flug einordnen">−</button><b>${adj > 0 ? "+" : ""}${esc(fmtNum(adj, adj % 1 ? 1 : 0))} s</b><button data-d="1" title="Video 1 s später im Flug einordnen">+</button>`;
    for (const b of line.querySelectorAll("button")) b.onclick = () => this._adjust(Number(b.dataset.d));

    // The enlarged player covers the charts: a small height curve stands in.
    const { p, t0, t1 } = ctx;
    const hx = (t) => (1000 * (t - t0)) / (t1 - t0 || 1);
    const heights = p.height || [];
    const hi = Math.max(1, ...heights.filter((v) => v != null));
    let d = "";
    let pen = false;
    heights.forEach((v, i) => {
      if (v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${hx(p.t[i]).toFixed(1)},${(38 - (36 * Math.max(0, v)) / hi).toFixed(1)}`;
      pen = true;
    });
    const a = hx(Math.max(t0, s.offset));
    const b = hx(Math.min(t1, s.offset + (s.duration || s.video.duration || 0)));
    hud.innerHTML = `
      <div id="hudvals">&nbsp;</div>
      <svg viewBox="0 0 1000 40" preserveAspectRatio="none">
        ${b > a ? `<rect class="range" x="${a}" y="0" width="${b - a}" height="40"></rect>` : ""}
        <path d="${d}" vector-effect="non-scaling-stroke"></path>
        <line class="cur" y1="0" y2="40" vector-effect="non-scaling-stroke" visibility="hidden"></line>
      </svg>`;
    const mini = hud.querySelector("svg");
    mini.onclick = (e) => {
      const r = mini.getBoundingClientRect();
      this._seekTo(t0 + ((e.clientX - r.left) / (r.width || 1)) * (t1 - t0), this._mediaIndex);
    };
  }

  _setBig(big) {
    const stage = this.shadowRoot.getElementById("stage");
    if (!stage || stage.classList.contains("big") === big) return;
    stage.classList.toggle("big", big);
    const btn = stage.querySelector(".grow");
    if (btn) {
      btn.title = big ? "Verkleinern" : "Vergrößern";
      btn.innerHTML = svg(big ? ICON_SHRINK : ICON_EXPAND);
    }
    // A photo swaps between cover and full picture; a video keeps playing as is.
    const m = this._recordings?.[this._mediaIndex];
    const img = stage.querySelector(".screen img");
    if (img && m?.play) img.src = big ? m.play : m.thumb || m.play;
    this._onKey ??= (e) => {
      if (e.key === "Escape") this._setBig(false);
    };
    if (big) window.addEventListener("keydown", this._onKey);
    else window.removeEventListener("keydown", this._onKey);
  }

  // -- charts -----------------------------------------------------------------

  _renderCharts() {
    const el = this.shadowRoot.getElementById("charts");
    if (!el) return;
    this._chartCtx = null;
    this._hoverT = null;
    const p = this._track?.profile;
    if (!p || !p.t?.length) {
      this._updateSyncUi();
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

    const build = (chartH) => {
      let html = this._bandHtml(x, width) + this._recBandHtml(x, width, t0, t1);
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
        const y = (v) => 4 + (1 - (v - lo) / (hi - lo)) * (chartH - 8);
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
            <svg width="${width}" height="${chartH}">
              <line class="grid" x1="${PAD_L}" x2="${width - PAD_R}" y1="${y(hi)}" y2="${y(hi)}"></line>
              <line class="grid" x1="${PAD_L}" x2="${width - PAD_R}" y1="${y(lo)}" y2="${y(lo)}"></line>
              <text x="${PAD_L - 6}" y="${y(hi) + 3}" text-anchor="end">${esc(fmtNum(hi, hi - lo < 10 ? 1 : 0))}</text>
              <text x="${PAD_L - 6}" y="${y(lo) + 3}" text-anchor="end">${esc(fmtNum(lo, hi - lo < 10 ? 1 : 0))}</text>
              ${eventLines(chartH)}
              <path d="${d}" fill="none" stroke="${c.color}" stroke-width="1.8" stroke-linejoin="round"></path>
              <line class="cursor" x1="0" x2="0" y1="0" y2="${chartH}" visibility="hidden"></line>
              <circle r="3.5" fill="${c.color}" visibility="hidden"></circle>
            </svg>
          </div>`;
        this._charts.push({ ...c, y, vals });
      }
      html += this._axisHtml(x, t0, t1, width);
      el.innerHTML = `<div class="charts">${html}</div>`;
    };
    // Split layout: #charts has the pane's height; share it between the charts.
    const avail = this.classList.contains("narrow") ? 0 : el.clientHeight;
    let chartH = avail ? this._chartH || CHART_H : CHART_H;
    build(chartH);
    const n = this._charts.length;
    if (avail && n) {
      const fixed = el.firstElementChild.offsetHeight - n * chartH;
      const fit = Math.floor(Math.min(MAX_CHART_H, Math.max(MIN_CHART_H, (avail - fixed) / n)));
      if (fit !== chartH) build((chartH = fit));
      this._chartH = chartH;
    }
    this._chartBox = { w: width, h: el.clientHeight };
    this._wireCharts(el.querySelector(".charts"), p, x, t0, t1, plotW);
    this._observe(el);
    this._markSelected();
    this._updateSyncUi();
    this._refreshCursor();
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

  /** Band with the recordings: videos as bars over their time, photos as dots. */
  _recBandHtml(x, width, t0, t1) {
    const items = (this._recordings || [])
      .map((m, i) => {
        const a = this._recOffset(m);
        if (a == null) return "";
        const len = m.kind !== "photo" ? this._recDuration(m) : null;
        const b = len ? a + len : null;
        const title = `<title>${esc(`${m.name} · ${fmtClock(a)}${b != null ? `–${fmtClock(b)}` : ""}`)}</title>`;
        if (b == null) {
          return a < t0 || a > t1 ? "" : `<circle data-rec="${i}" cx="${x(a)}" cy="7" r="4">${title}</circle>`;
        }
        if (b < t0 || a > t1) return "";
        const xa = x(Math.max(a, t0));
        return `<rect data-rec="${i}" x="${xa}" y="2" width="${Math.max(3, x(Math.min(b, t1)) - xa)}" height="10" rx="2">${title}</rect>`;
      })
      .join("");
    if (!items) return "";
    return `
      <div class="band recband">
        <svg width="${width}" height="14"><text x="${PAD_L - 6}" y="11" text-anchor="end">Medien</text>${items}</svg>
      </div>`;
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

  _wireCharts(root, p, x, t0, t1, plotW) {
    const labels = [...root.querySelectorAll(".chart .val")];
    this._chartCtx = {
      p,
      x,
      t0,
      t1,
      svgs: [...root.querySelectorAll(".chart svg")],
      labels,
      peaks: labels.map((l) => l.innerHTML),
      shown: true, // so the first update clears a cursor left on the map
    };
    const timeAt = (e) => {
      const px = e.clientX - root.getBoundingClientRect().left;
      if (px < PAD_L - 4 || px > PAD_L + plotW + 4) return null;
      return Math.min(t1, Math.max(t0, t0 + ((px - PAD_L) / plotW) * (t1 - t0)));
    };
    // Hovering wins over the video; leaving the charts hands the cursor back to it.
    root.onpointermove = (e) => {
      this._hoverT = timeAt(e);
      this._refreshCursor();
    };
    root.onpointerleave = () => {
      this._hoverT = null;
      this._refreshCursor();
    };
    root.onclick = (e) => {
      const t = timeAt(e);
      const hit = e.target.closest?.("[data-rec]");
      if (t != null) this._seekTo(t, hit ? Number(hit.dataset.rec) : null);
    };
  }

  _refreshCursor() {
    this._setCursor(this._hoverT ?? this._videoTime() ?? this._photoT);
  }

  /** Cursor in all charts, on the map and in the enlarged player at flight second t; null hides it. */
  _setCursor(t) {
    const ctx = this._chartCtx;
    if (!ctx) return;
    const { p, x, svgs, labels, peaks } = ctx;
    const hudVals = this.shadowRoot.getElementById("hudvals");
    const hudCur = this.shadowRoot.querySelector("#hud .cur");
    if (t == null || t < ctx.t0 || t > ctx.t1) {
      if (!ctx.shown) return;
      ctx.shown = false;
      svgs.forEach((svg) => {
        svg.querySelector(".cursor").setAttribute("visibility", "hidden");
        svg.querySelector("circle").setAttribute("visibility", "hidden");
      });
      labels.forEach((l, k) => (l.innerHTML = peaks[k]));
      this._map?.setCursor?.(null, null);
      if (hudVals) hudVals.innerHTML = "&nbsp;";
      hudCur?.setAttribute("visibility", "hidden");
      return;
    }
    ctx.shown = true;
    const [i, f] = locate(p.t, t);
    const cx = x(t);
    const clock = esc(fmtClock(t));
    const parts = [clock];
    this._charts.forEach((c, k) => {
      const svg = svgs[k];
      const line = svg.querySelector(".cursor");
      const dot = svg.querySelector("circle");
      line.setAttribute("x1", cx);
      line.setAttribute("x2", cx);
      line.setAttribute("visibility", "visible");
      const v = lerpAt(c.vals, i, f);
      if (v == null) {
        dot.setAttribute("visibility", "hidden");
        labels[k].innerHTML = `${clock} · –`;
      } else {
        const text = esc(fmtNum(v, c.digits, c.unit));
        dot.setAttribute("cx", cx);
        dot.setAttribute("cy", c.y(v));
        dot.setAttribute("visibility", "visible");
        labels[k].innerHTML = `${clock} · <b>${text}</b>`;
        parts.push(`${esc(c.short)} <b>${text}</b>`);
      }
    });
    this._map?.setCursor?.(lerpAt(p.lat, i, f), lerpAt(p.lon, i, f));
    if (hudVals) hudVals.innerHTML = parts.join(" · ");
    if (hudCur) {
      const hx = (1000 * (t - ctx.t0)) / (ctx.t1 - ctx.t0 || 1);
      hudCur.setAttribute("x1", hx);
      hudCur.setAttribute("x2", hx);
      hudCur.setAttribute("visibility", "visible");
    }
  }

  _observe(el) {
    if (this._ro) return;
    this._ro = new ResizeObserver(() => {
      this._layout(); // the key figures may wrap into another row
      const target = this.shadowRoot.getElementById("charts");
      if (!target?.clientWidth) return;
      const box = this._chartBox;
      const narrow = this.classList.contains("narrow");
      if (Math.abs(target.clientWidth - box.w) > 2 || (!narrow && Math.abs(target.clientHeight - box.h) > 2)) this._renderCharts();
    });
    this._ro.observe(this);
    this._ro.observe(el);
  }
}

const KIND_LABEL = { video: "Video", "360": "360°", photo: "Foto" };
const ICON_EXPAND = "M10,21V19H6.41L10.91,14.5L9.5,13.09L5,17.59V14H3V21H10M14.5,10.91L19,6.41V10H21V3H14V5H17.59L13.09,9.5L14.5,10.91Z";
const ICON_SHRINK = "M19.5,3.09L15,7.59V4H13V11H20V9H16.41L20.91,4.5L19.5,3.09M4,13V15H7.59L3.09,19.5L4.5,20.91L9,16.41V20H11V13H4Z";

function svg(path) {
  return `<svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor"><path d="${path}"/></svg>`;
}

function table(rows) {
  return `<table>${rows
    .map(([k, v, cls = ""]) => `<tr><td>${esc(k)}</td><td class="${cls}">${esc(v)}</td></tr>`)
    .join("")}</table>`;
}

function modeColor(label) {
  return MODE_COLORS[label] || OTHER_MODE_COLOR;
}

/** [i, f]: t lies between samples i and i + 1 of the ascending array ts, at fraction f. */
function locate(ts, t) {
  if (ts.length < 2) return [0, 0];
  let lo = 0;
  let hi = ts.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) lo = mid;
    else hi = mid;
  }
  const span = ts[hi] - ts[lo];
  return [lo, span > 0 ? Math.min(1, Math.max(0, (t - ts[lo]) / span)) : 0];
}

/** Value between samples i and i + 1 at fraction f; the nearer one if the other is missing. */
function lerpAt(vals, i, f) {
  const a = vals?.[i];
  const b = vals?.[i + 1];
  if (a == null || b == null) return (f < 0.5 ? a : b) ?? null;
  return a + (b - a) * f;
}

function readLayout() {
  try {
    const v = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

function writeLayout(prefs) {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(prefs));
  } catch {
    // No storage (private window): the sizes hold until the page is left.
  }
}

/** Nearest scrolling ancestor, across shadow roots; null for the page itself. */
function scrollParent(el) {
  for (let n = el.parentNode || el.host; n; n = n.parentNode || n.host) {
    if (n instanceof Element && /(auto|scroll)/.test(getComputedStyle(n).overflowY) && n.clientHeight) return n;
  }
  return null;
}

/** Manual shift of a recording in seconds (see ADJUST_KEY). */
function readAdjust(id) {
  try {
    return Number(localStorage.getItem(ADJUST_KEY + id)) || 0;
  } catch {
    return 0;
  }
}

if (!customElements.get("dji-flight-details")) {
  customElements.define("dji-flight-details", DjiFlightDetails);
}
