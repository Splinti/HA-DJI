/*
 * dji-flightlog-panel
 *
 * Full-page Home Assistant panel (sidebar entry) for the dji_flightlog
 * integration, in three views:
 *   Flüge   statistics, filters, a large map and a clickable flight list
 *   Flug    details of one flight (dji-flight-details: charts, battery, ...)
 *   Planen  a map with the DIPUL geo zones for picking and saving spots, the
 *           place search and the saved spots; ``?spot=<id>`` opens a spot here
 * Flight records can be uploaded with the upload button or by dropping files /
 * folders onto the page (admins only).
 *
 * Registered by the integration via panel_custom; the maps are
 * dji-flight-map-card elements, loaded on demand like the detail view.
 *
 * With a media source (OneDrive, folder) connected, every flight carries the recordings
 * made during it (`flight.media`); the selected flight shows them as a
 * strip of thumbnails that play in an overlay or open at the source.
 */

const STATIC = "/dji_flightlog_static";
const API = "dji_flightlog";

// Same ?v= as this module (set by the integration), so an update is not
// served stale modules from the browser cache.
const VERSION_QUERY = new URL(import.meta.url).search;
let cardPromise = null;
function loadCard() {
  if (customElements.get("dji-flight-map-card")) return Promise.resolve();
  cardPromise ??= import(`${STATIC}/dji-flight-map-card.js${VERSION_QUERY}`);
  return cardPromise;
}
// The card module also holds the labels for flight controller actions.
let cardModulePromise = null;
function loadCardModule() {
  cardModulePromise ??= import(`${STATIC}/dji-flight-map-card.js${VERSION_QUERY}`).catch(() => null);
  return cardModulePromise;
}
let detailsPromise = null;
function loadDetails() {
  detailsPromise ??= import(`${STATIC}/dji-flight-details.js${VERSION_QUERY}`);
  return detailsPromise;
}

const VIEWS = ["flights", "flight", "plan"];
const VIEW_KEY = "dji_flightlog.panel_view";
function loadView() {
  try {
    const v = localStorage.getItem(VIEW_KEY);
    return VIEWS.includes(v) ? v : null;
  } catch {
    return null;
  }
}
function saveView(v) {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch {
    // Private mode or blocked storage: the panel just starts on "Flüge".
  }
}

const fmtDate = (iso, opts = { dateStyle: "medium", timeStyle: "short" }) =>
  iso ? new Date(iso).toLocaleString(undefined, opts) : "";
const fmtDay = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "full" }) : "");
const fmtDur = (s) => {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m}:${String(s % 60).padStart(2, "0")} min`;
};
const fmtDist = (m) => (m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m || 0)} m`);
const fmtClock = (iso) => (iso ? new Date(iso).toLocaleTimeString(undefined, { timeStyle: "short" }) : "");
const KIND_LABEL = { video: "Video", "360": "360°", photo: "Foto" };
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

const RANGES = [
  { value: "0", label: "Alle" },
  { value: "7", label: "7 Tage" },
  { value: "30", label: "30 Tage" },
  { value: "90", label: "3 Monate" },
  { value: "365", label: "1 Jahr" },
];

class DjiFlightLogPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._rendered = false;
    this._data = null;
    this._selected = null;
    this._loading = false;
    this._lastKey = null;
    this._filters = { days: "0", aircraft: "", heatmap: false, dipul: false };
    this._view = loadView() || "flights";
    this._planFlights = false;
    this._spots = [];
    this._spotsKey = null;
    this._reload = false;
    this._uploading = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._rendered) this._render();
    this.shadowRoot.getElementById("upload").hidden = !hass.user?.is_admin;
    // Only once the cards are upgraded and configured (see _setupCard).
    if (this._cardReady) this._card.hass = hass;
    if (this._planReady) this._planCard.hass = hass;
    if (this._details) this._details.hass = hass;
    // Reload whenever the integration reports a new import.
    const ent = hass.states["sensor.dji_flight_log_last_import"];
    const key = ent ? ent.state : "none";
    if (first || key !== this._lastKey) {
      this._lastKey = key;
      this._load();
    }
    const spots = hass.states["sensor.dji_flight_log_saved_spots"];
    const spotsKey = spots ? spots.last_updated : "none";
    if (first || spotsKey !== this._spotsKey) {
      this._spotsKey = spotsKey;
      this._loadSpots();
    }
  }

  get hass() {
    return this._hass;
  }

  set narrow(value) {
    this._narrow = value;
    this.shadowRoot?.host?.classList?.toggle("narrow", !!value);
    if (this._rendered) this.shadowRoot.querySelector(".layout")?.classList.toggle("narrow", !!value);
    this._details?.classList.toggle("narrow", !!value);
  }

  set panel(_panel) {
    // Config from panel_custom; nothing configurable yet.
  }

  set route(_route) {
    // Also called when a dashboard card links here with ?spot=<id>.
    if (this._rendered) this._openSpotFromUrl();
  }

  _fireMenu() {
    this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
  }

  _render() {
    this._rendered = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          /* HA's panel container has no height of its own, so a height: 100%
             would resolve to auto and the panel would be as tall as its
             content. */
          height: 100vh;
          height: 100dvh;
          /* auto, not hidden: on very short windows scrolling beats cutting
             off the flight list (.mapwrap has min-height: 260px). */
          overflow: auto;
          background: var(--primary-background-color, #fafafa);
          color: var(--primary-text-color, #212121);
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
          --header-h: 56px;
        }
        .page { display: flex; flex-direction: column; height: 100%; box-sizing: border-box; }
        header {
          display: flex; align-items: center; gap: 12px;
          height: var(--header-h); padding: 0 16px; flex: 0 0 auto;
          background: var(--app-header-background-color, var(--primary-color));
          color: var(--app-header-text-color, #fff);
          padding-top: env(safe-area-inset-top, 0px);
          box-sizing: content-box;
        }
        header .title { font-size: 20px; font-weight: 400; flex: 1; }
        header button, .filters button {
          background: none; border: none; color: inherit; cursor: pointer;
          padding: 6px; border-radius: 50%; line-height: 0;
        }
        header button:hover { background: rgba(255,255,255,0.12); }
        header button.active { background: rgba(255,255,255,0.24); }
        header button.busy svg { animation: spin 1s linear infinite; }
        header button#upload.busy svg { animation: pulse 1s ease-in-out infinite alternate; }
        @keyframes pulse { to { opacity: 0.3; } }
        @keyframes spin { to { transform: rotate(360deg); } }
        #menu { display: none; }
        .layout.narrow #menu { display: inline-flex; }

        nav.views {
          display: flex; flex: 0 0 auto; padding: 0 8px; overflow-x: auto;
          background: var(--app-header-background-color, var(--primary-color));
          color: var(--app-header-text-color, #fff);
        }
        nav.views button {
          background: none; border: none; border-bottom: 2px solid transparent; color: inherit; cursor: pointer;
          font: inherit; font-size: 14px; font-weight: 500; padding: 10px 16px; opacity: 0.7;
        }
        nav.views button:hover { opacity: 1; }
        nav.views button.on { opacity: 1; border-bottom-color: currentColor; }
        .view { flex: 1 1 auto; display: flex; flex-direction: column; min-height: 0; }
        .view[hidden] { display: none; }
        #v-flight { overflow-y: auto; }

        .stats { display: flex; flex-wrap: wrap; gap: 12px; padding: 12px 16px 0; flex: 0 0 auto; }
        .tile {
          flex: 1 1 130px; background: var(--card-background-color, #fff);
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
          padding: 12px 16px;
        }
        .tile .v { font-size: 22px; font-weight: 500; }
        .tile .k { font-size: 12px; color: var(--secondary-text-color, #727272); margin-top: 2px; }

        .filters { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 12px 16px 0; }
        .filters label { font-size: 13px; color: var(--secondary-text-color); display: flex; align-items: center; gap: 6px; }
        .filters select {
          font: inherit; font-size: 13px; padding: 6px 8px; border-radius: 8px;
          background: var(--card-background-color, #fff); color: inherit;
          border: 1px solid var(--divider-color, #e0e0e0);
        }
        .search { position: relative; flex: 1 1 240px; max-width: 420px; }
        .search input {
          width: 100%; box-sizing: border-box; font: inherit; font-size: 14px; padding: 7px 10px;
          border-radius: 8px; border: 1px solid var(--divider-color, #e0e0e0);
          background: var(--card-background-color, #fff); color: inherit;
        }
        .search.busy input { opacity: 0.6; }
        #results {
          position: absolute; left: 0; right: 0; top: calc(100% + 4px); z-index: 5;
          background: var(--card-background-color, #fff); border-radius: 8px; overflow: hidden;
          box-shadow: 0 4px 12px rgba(0,0,0,.2); font-size: 13px;
        }
        #results[hidden] { display: none; }
        #results button {
          display: block; width: 100%; text-align: left; font: inherit; color: inherit;
          background: none; border: none; padding: 8px 12px; cursor: pointer;
        }
        #results button:hover, #results button:focus { background: var(--secondary-background-color, #f2f2f2); outline: none; }
        #results .msg, #results .src { padding: 8px 12px; color: var(--secondary-text-color); }
        #results .src { font-size: 11px; padding-top: 4px; border-top: 1px solid var(--divider-color, #e0e0e0); }

        .body { flex: 1 1 auto; display: flex; gap: 12px; padding: 12px 16px 16px; min-height: 0; box-sizing: border-box; }
        /* isolate: keeps Leaflet's pane z-indices (400+) below the search results. */
        .mapwrap { flex: 1 1 auto; min-width: 0; min-height: 260px; display: flex; isolation: isolate; }
        .mapwrap dji-flight-map-card { flex: 1 1 auto; display: block; min-width: 0; min-height: 0; }
        aside {
          flex: 0 0 320px; display: flex; flex-direction: column; overflow: hidden;
          background: var(--card-background-color, #fff);
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
        }
        #list, #spotlist { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
        .asidehead {
          flex: 0 0 auto; padding: 10px 16px; font-size: 14px; font-weight: 500;
          border-bottom: 1px solid var(--divider-color, #e0e0e0);
        }
        /* Phone: the page scrolls, the map gets a fixed share of the screen. */
        :host(.narrow) { height: auto; min-height: 100dvh; }
        .layout.narrow .body { flex-direction: column; }
        :host(.narrow) #v-flight { overflow: visible; }
        .layout.narrow .mapwrap { flex: 0 0 auto; height: 60vh; }
        .layout.narrow aside { flex: 0 0 auto; max-height: 60vh; }

        .day { padding: 10px 16px 4px; font-size: 12px; font-weight: 500; color: var(--secondary-text-color); position: sticky; top: 0; background: var(--card-background-color, #fff); }
        .row { display: flex; align-items: center; gap: 10px; padding: 10px 16px; cursor: pointer; border-left: 4px solid transparent; }
        .row:hover { background: var(--secondary-background-color, #f2f2f2); }
        .row.sel { background: var(--secondary-background-color, #f2f2f2); border-left-color: var(--primary-color); }
        .row .dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
        .row .main { flex: 1; min-width: 0; }
        .row .t { font-size: 14px; }
        .row .d { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .row .warn { font-size: 11px; color: var(--warning-color, #ffa600); }
        .row .warn.crit { color: var(--error-color, #db4437); }
        .row .pin { flex: 0 0 auto; color: #ff9800; line-height: 0; }
        .row .act { flex: 0 0 auto; display: inline-flex; color: var(--secondary-text-color); padding: 6px; border-radius: 50%; background: none; border: none; cursor: pointer; line-height: 0; }
        .row .act:hover { background: var(--divider-color, #e0e0e0); color: var(--primary-text-color); }
        .row a.act { color: var(--primary-color); }
        .hint { padding: 12px 16px; font-size: 13px; color: var(--secondary-text-color); }
        .row .badge { flex: 0 0 auto; font-size: 12px; color: var(--secondary-text-color); display: flex; align-items: center; gap: 2px; }
        .row .badge svg { width: 16px; height: 16px; }
        .row .hint { padding: 0; font-size: 11px; color: var(--secondary-text-color); }

        .media { display: flex; gap: 8px; overflow-x: auto; padding: 0 16px 12px 34px; background: var(--secondary-background-color, #f2f2f2); }
        .media .m { flex: 0 0 136px; cursor: pointer; }
        .media .thumb {
          position: relative; width: 136px; height: 77px; border-radius: 6px; overflow: hidden;
          background: var(--divider-color, #ddd); display: flex; align-items: center; justify-content: center;
          color: var(--secondary-text-color); font-size: 12px;
        }
        .media .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
        .media .thumb .k, .media .thumb .p {
          position: absolute; font-size: 11px; line-height: 1; padding: 3px 5px; border-radius: 4px;
          background: rgba(0,0,0,.6); color: #fff;
        }
        .media .thumb .k { left: 4px; top: 4px; }
        .media .thumb .p { right: 4px; bottom: 4px; }
        .media .m:hover .thumb { outline: 2px solid var(--primary-color); }
        .media .cap { display: flex; justify-content: space-between; font-size: 11px; color: var(--secondary-text-color); margin-top: 3px; }
        .media .cap a { color: var(--primary-color); text-decoration: none; }

        #player[hidden] { display: none; }
        #player {
          position: fixed; inset: 0; z-index: 10; background: rgba(0,0,0,.75);
          display: flex; align-items: center; justify-content: center; padding: 16px; box-sizing: border-box;
        }
        #player .box {
          width: min(1100px, 100%); max-height: 100%; display: flex; flex-direction: column;
          background: var(--card-background-color, #fff); border-radius: 12px; overflow: hidden;
        }
        #player .bar { display: flex; align-items: center; gap: 12px; padding: 8px 8px 8px 16px; }
        #player .bar .ttl { flex: 1; min-width: 0; font-size: 14px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        #player .bar a { font-size: 13px; color: var(--primary-color); text-decoration: none; white-space: nowrap; }
        #player .bar button { background: none; border: none; color: inherit; cursor: pointer; padding: 6px; border-radius: 50%; line-height: 0; }
        #player video, #player img.full { width: 100%; max-height: calc(100dvh - 140px); background: #000; display: block; object-fit: contain; }
        #player .hint { padding: 8px 16px 12px; font-size: 13px; color: var(--secondary-text-color); }
        .empty { padding: 24px 16px; color: var(--secondary-text-color); text-align: center; }
        .note {
          margin: 0 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px;
          background: var(--warning-color, #ffa600); color: #000;
        }
        .note a { color: inherit; }

        /* flex: none, or the column squeezes the box to make room for the map.
           Many notices scroll inside the box instead of pushing the map away. */
        #attention {
          flex: 0 0 auto; max-height: 40vh; overflow-y: auto;
          margin: 12px 16px 0; border-radius: var(--ha-card-border-radius, 12px);
          background: var(--card-background-color, #fff);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
        }
        #attention[hidden] { display: none; }
        #attention .ahead { display: flex; align-items: center; gap: 8px; padding: 10px 16px 4px; font-size: 14px; font-weight: 500; }
        #attention .ahead span { flex: 1; }
        #attention .item { display: flex; gap: 12px; align-items: flex-start; padding: 8px 16px; border-left: 4px solid var(--info-color, #039be5); }
        #attention .item.warning { border-left-color: var(--warning-color, #ffa600); }
        #attention .item.critical { border-left-color: var(--error-color, #db4437); }
        #attention .item .ic { line-height: 0; color: var(--info-color, #039be5); padding-top: 1px; }
        #attention .item.warning .ic { color: var(--warning-color, #ffa600); }
        #attention .item.critical .ic { color: var(--error-color, #db4437); }
        #attention .item .txt { flex: 1; min-width: 0; font-size: 14px; }
        #attention .item .sub { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
        #attention .item .sub a { color: var(--primary-color); cursor: pointer; }
        #attention button.done {
          flex: 0 0 auto; font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 6px; cursor: pointer;
          border: 1px solid var(--divider-color, #e0e0e0); background: none; color: var(--primary-text-color);
        }
        #attention button.done:hover { background: var(--secondary-background-color, #f2f2f2); }
        #attention .item + .item { border-top: 1px solid var(--divider-color, #e0e0e0); }

        #upbar {
          margin: 12px 16px 0; padding: 10px 14px; border-radius: 8px; font-size: 13px;
          background: var(--card-background-color, #fff);
          border-left: 4px solid var(--primary-color);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
          display: flex; gap: 8px; align-items: flex-start;
        }
        #upbar .txt { flex: 1; min-width: 0; }
        #upbar ul { margin: 6px 0 0; padding-left: 18px; color: var(--secondary-text-color); }
        #upbar li { overflow-wrap: anywhere; }
        #upbar button { background: none; border: none; cursor: pointer; color: inherit; padding: 2px; line-height: 0; }
        #upbar.problem { border-left-color: var(--warning-color, #ffa600); }
        #drop {
          position: fixed; inset: 0; z-index: 10;
          display: flex; align-items: center; justify-content: center;
          background: rgba(0, 0, 0, 0.45);
        }
        #drop div {
          pointer-events: none; /* dragleave only fires when leaving the overlay */
          padding: 32px 40px; border-radius: 16px; text-align: center; font-size: 18px;
          border: 3px dashed var(--primary-color);
          background: var(--card-background-color, #fff);
        }
        #upbar[hidden], #drop[hidden] { display: none; }
        #drop small { display: block; margin-top: 6px; font-size: 13px; color: var(--secondary-text-color); }
      </style>
      <div class="layout page">
        <header>
          <button id="menu" title="Menü">${svg("M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z")}</button>
          <div class="title">Drohnenflüge</div>
          <button id="upload" title="Flugaufzeichnungen hochladen (DJIFlightRecord_*.txt), oder Dateien auf die Seite ziehen" hidden>${svg(
            "M9,16V10H5L12,3L19,10H15V16H9M5,20V18H19V20H5Z",
          )}</button>
          <input type="file" id="file" accept=".txt" multiple hidden>
          <button id="scan" title="Log-Ordner jetzt scannen">${svg(
            "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
          )}</button>
        </header>
        <nav class="views">
          <button data-view="flights">Flüge</button>
          <button data-view="flight">Flug</button>
          <button data-view="plan">Planen</button>
        </nav>

        <div id="upbar" hidden></div>
        <div id="note" hidden></div>

        <section class="view" id="v-flights" hidden>
          <div id="attention" hidden></div>
          <div class="stats" id="stats"></div>
          <div class="filters">
            <label>Zeitraum
              <select id="range">${RANGES.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}</select>
            </label>
            <label id="aclabel" hidden>Drohne
              <select id="aircraft"><option value="">Alle</option></select>
            </label>
            <label><input type="checkbox" id="heat"> Heatmap</label>
            <label title="Geografische Gebiete für Drohnen (DFS / dipul)"><input type="checkbox" id="dipul"> DIPUL-Zonen</label>
          </div>
          <div class="body">
            <div class="mapwrap"><dji-flight-map-card id="map"></dji-flight-map-card></div>
            <aside>
              <div class="asidehead">Flüge</div>
              <div id="list"></div>
            </aside>
          </div>
        </section>

        <section class="view" id="v-flight" hidden></section>

        <section class="view" id="v-plan" hidden>
          <div class="filters">
            <form class="search" id="search" role="search">
              <input type="search" id="q" placeholder="PLZ, Ort, Adresse oder Koordinaten" autocomplete="off"
                title="z. B. 80331, Marienplatz München, 48.13743, 11.57549 oder 48°08'14.7&quot;N 11°34'31.8&quot;E">
              <div id="results" hidden></div>
            </form>
            <label title="Die Tracks der bisherigen Flüge auf der Planungskarte zeigen"><input type="checkbox" id="planflights"> Flüge einblenden</label>
          </div>
          <div class="body">
            <div class="mapwrap" id="planwrap"><dji-flight-map-card id="planmap"></dji-flight-map-card></div>
            <aside>
              <div class="asidehead" id="spotshead">Gemerkte Orte</div>
              <div id="spotlist"></div>
            </aside>
          </div>
        </section>
      </div>
      <div id="drop" hidden><div>Flugaufzeichnungen hier ablegen<small>DJIFlightRecord_*.txt oder der Ordner FlightRecord</small></div></div>
      <div id="player" hidden></div>`;

    this.shadowRoot.getElementById("menu").onclick = () => this._fireMenu();
    this.shadowRoot.getElementById("scan").onclick = () => this._scan();
    const fileInput = this.shadowRoot.getElementById("file");
    this.shadowRoot.getElementById("upload").onclick = () => fileInput.click();
    fileInput.onchange = () => {
      const files = [...fileInput.files];
      fileInput.value = ""; // picking the same files again must fire change
      this._upload(files);
    };
    this._setupDrop();
    this.shadowRoot.getElementById("range").onchange = (e) => {
      this._filters.days = e.target.value;
      this._applyFilters();
    };
    this.shadowRoot.getElementById("aircraft").onchange = (e) => {
      this._filters.aircraft = e.target.value;
      this._applyFilters();
    };
    this.shadowRoot.getElementById("heat").onchange = (e) => {
      this._filters.heatmap = e.target.checked;
      this._card?.updateOptions({ heatmap: e.target.checked });
    };
    this.shadowRoot.getElementById("dipul").onchange = (e) => {
      this._filters.dipul = e.target.checked;
      this._card?.setDipul(e.target.checked);
    };
    this._setupSearch();
    this.shadowRoot.getElementById("planflights").onchange = (e) => {
      this._planFlights = e.target.checked;
      if (this._planReady) this._planCard.updateOptions({ flights: this._planFlights });
    };
    for (const b of this.shadowRoot.querySelectorAll("nav.views button")) {
      b.onclick = () => this._setView(b.dataset.view);
    }
    // Fired by the map card after a spot was saved, re-checked or deleted.
    this.shadowRoot.addEventListener("dji-spots-changed", () => this._loadSpots());
    // "Details" in a flight's popup on the overview map.
    this.shadowRoot.addEventListener("dji-flight-details", (e) => this._openDetails(e.detail.flight_id));
    // Prev/next inside the detail view: keep the list selection in step.
    this.shadowRoot.addEventListener("dji-flight-selected", (e) => {
      this._selected = e.detail.flight_id;
      this._renderList();
      if (this._cardReady) this._card.focusFlight(this._selected, { openPopup: false });
    });

    this.shadowRoot.querySelector(".layout").classList.toggle("narrow", !!this._narrow);
    this._setupCard();
    // ?spot=<id> (a dashboard card linking here) wins over the remembered view.
    this._setView(new URL(location.href).searchParams.has("spot") ? "plan" : this._view);

    this._onKey = (e) => {
      if (e.key === "Escape") this._closePlayer();
    };
  }

  disconnectedCallback() {
    this._closePlayer();
    this._details?.pause?.();
  }

  get _card() {
    return this.shadowRoot?.getElementById("map");
  }

  get _planCard() {
    return this.shadowRoot?.getElementById("planmap");
  }

  get _details() {
    return this.shadowRoot?.querySelector("dji-flight-details");
  }

  async _setupCard() {
    await loadCard();
    const card = this._card;
    if (!card) return;
    // HA may not have attached the panel yet, and custom elements in a
    // detached tree are only upgraded on request.
    customElements.upgrade(card);
    card.setConfig({
      title: "",
      mode: "all",
      height: 100, // overridden by CSS; the card fills the flex area
      heatmap: this._filters.heatmap,
      scan_button: false,
      refresh_seconds: 0, // the panel drives reloads
      fit: true,
      spots: true,
      details: true,
      dipul: this._filters.dipul,
    });
    fillPanel(card);
    this._cardReady = true;
    if (this._hass) {
      card.hass = this._hass;
      // Force the first load instead of relying on a refresh-entity change.
      card.updateOptions(this._cardFilters());
    }
  }

  /** The planning map is built on first use: Leaflet cannot size itself in a hidden view. */
  async _setupPlanCard() {
    if (this._planSetup) return;
    this._planSetup = true;
    await loadCard();
    const card = this._planCard;
    if (!card) return;
    customElements.upgrade(card);
    card.setConfig({
      title: "",
      mode: "all",
      height: 100,
      flights: this._planFlights,
      scan_button: false,
      refresh_seconds: 0,
      fit: true,
      spots: true,
      details: true,
      dipul: true,
    });
    fillPanel(card);
    this._planReady = true;
    if (this._hass) card.hass = this._hass;
    // Planning mode: a tap on the map picks a spot and shows its DIPUL zones.
    card.setPlanning(true);
    this._openSpotFromUrl();
  }

  _setView(view) {
    if (!VIEWS.includes(view)) view = "flights";
    this._view = view;
    saveView(view);
    for (const b of this.shadowRoot.querySelectorAll("nav.views button")) b.classList.toggle("on", b.dataset.view === view);
    for (const v of VIEWS) this.shadowRoot.getElementById(`v-${v}`).hidden = v !== view;
    if (view !== "flight") this._details?.pause?.(); // a hidden video would keep playing
    if (view === "plan") {
      this._setupPlanCard();
      this._renderSpots();
    } else if (view === "flight") {
      this._showDetails();
    }
  }

  _openDetails(flightId) {
    this._selected = flightId;
    this._renderList();
    this._setView("flight");
  }

  async _showDetails() {
    const host = this.shadowRoot.getElementById("v-flight");
    if (!this._details) {
      await loadDetails();
      if (!this._details) host.innerHTML = "<dji-flight-details></dji-flight-details>";
      const el = this._details;
      customElements.upgrade(el);
      el.classList.toggle("narrow", !!this._narrow);
      if (this._hass) el.hass = this._hass;
    }
    const flights = this._data?.flights || [];
    this._details.setFlights(flights);
    // Nothing picked yet: the newest flight.
    const id = this._selected && flights.some((f) => f.flight_id === this._selected) ? this._selected : flights[0]?.flight_id;
    if (id !== this._detailsId) {
      this._detailsId = id;
      this._details.show(id || null);
    }
  }

  async _loadSpots() {
    if (!this._hass) return;
    try {
      const res = await this._hass.callApi("GET", `${API}/spots`);
      this._spots = res.spots || [];
    } catch (err) {
      console.error("dji-flightlog-panel spots:", err);
      return;
    }
    const head = this.shadowRoot.getElementById("spotshead");
    if (head) head.textContent = this._spots.length ? `Gemerkte Orte (${this._spots.length})` : "Gemerkte Orte";
    this._renderSpots();
    this._openSpotFromUrl();
  }

  _openSpotFromUrl() {
    const url = new URL(location.href);
    const id = url.searchParams.get("spot");
    if (!id) return;
    if (this._view !== "plan") this._setView("plan"); // builds the planning map, which calls back here
    if (!this._planReady) return;
    url.searchParams.delete("spot");
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
    this._planCard.focusSpot(id);
  }

  async _deleteSpot(spot) {
    if (!confirm(`„${spot.name}“ löschen?`)) return;
    try {
      await this._hass.callApi("DELETE", `${API}/spots/${spot.id}`);
    } catch (err) {
      console.error("dji-flightlog-panel delete spot:", err);
      return;
    }
    await this._loadSpots();
    if (this._cardReady) this._card.reloadSpots();
    if (this._planReady) this._planCard.reloadSpots();
  }

  _renderSpots() {
    const list = this.shadowRoot.getElementById("spotlist");
    const intro = `<div class="hint">Auf die Karte tippen, Namen eingeben, „Merken“. Die DIPUL-Zonen erscheinen ab Zoomstufe 8 und sind nur zur Orientierung: vor dem Flug auf dipul.de prüfen.</div>`;
    if (!this._spots.length) {
      list.innerHTML = intro + `<div class="empty">Noch keine Orte gemerkt.</div>`;
      return;
    }
    list.innerHTML =
      intro +
      this._spots
        .map((s) => {
          const zones =
            s.zones == null
              ? "Zonen nicht geprüft"
              : s.zones.length
                ? `${s.zones.length} DIPUL-Zone${s.zones.length === 1 ? "" : "n"}`
                : "keine DIPUL-Zone";
          return `
          <div class="row" data-id="${esc(s.id)}">
            <div class="pin">${svg("M12,2C8.13,2 5,5.13 5,9C5,14.25 12,22 12,22C12,22 19,14.25 19,9C19,5.13 15.87,2 12,2Z")}</div>
            <div class="main">
              <div class="t">${esc(s.name)}</div>
              <div class="d">${esc(zones)}${s.note ? ` · ${esc(s.note)}` : ""}</div>
            </div>
            <a class="act" href="${esc(s.maps_url)}" target="_blank" rel="noopener" title="Navigation mit Google Maps">${svg(
              "M12,2L4.5,20.29L5.21,21L12,18L18.79,21L19.5,20.29L12,2Z",
            )}</a>
            <button class="act del" title="Löschen">${svg(
              "M19,4H15.5L14.5,3H9.5L8.5,4H5V6H19M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19Z",
            )}</button>
          </div>`;
        })
        .join("");
    for (const row of list.querySelectorAll(".row")) {
      const spot = this._spots.find((s) => s.id === row.dataset.id);
      row.onclick = () => this._planReady && this._planCard.focusSpot(spot.id);
      row.querySelector("a.act").onclick = (ev) => ev.stopPropagation();
      row.querySelector(".del").onclick = (ev) => {
        ev.stopPropagation();
        this._deleteSpot(spot);
      };
    }
  }

  _cardFilters() {
    return {
      days: this._filters.days === "0" ? null : Number(this._filters.days),
      aircraft: this._filters.aircraft || null,
      heatmap: this._filters.heatmap,
    };
  }

  _query() {
    const q = new URLSearchParams();
    if (this._filters.days !== "0") {
      q.set("since", new Date(Date.now() - Number(this._filters.days) * 86400e3).toISOString());
    }
    if (this._filters.aircraft) q.set("aircraft", this._filters.aircraft);
    return q;
  }

  async _load() {
    if (!this._hass) return;
    if (this._loading) {
      this._reload = true; // run once more when the current load is done
      return;
    }
    this._loading = true;
    try {
      this._data = await this._hass.callApi("GET", `${API}/flights?${this._query()}`);
      this._renderStats();
      this._renderList();
      this._renderNote();
      this._renderAttention();
      if (this._view === "flight") this._showDetails();
      else this._details?.setFlights(this._data.flights || []);
    } catch (err) {
      console.error("dji-flightlog-panel:", err);
      const list = this.shadowRoot.getElementById("list");
      if (list) list.innerHTML = `<div class="empty">Fehler: ${esc(err.message || err)}</div>`;
    } finally {
      this._loading = false;
      if (this._reload) {
        this._reload = false;
        this._load();
      }
    }
  }

  _applyFilters() {
    this._selected = null;
    this._card?.updateOptions(this._cardFilters());
    this._load();
  }

  async _scan() {
    const btn = this.shadowRoot.getElementById("scan");
    if (!btn || btn.classList.contains("busy")) return;
    btn.classList.add("busy");
    try {
      await this._hass.callService(API, "scan", {});
      setTimeout(() => {
        this._load();
        if (this._cardReady) this._card.updateOptions({});
        if (this._planReady && this._planFlights) this._planCard.updateOptions({});
      }, 3000);
    } catch (err) {
      console.error("dji-flightlog-panel scan:", err);
    } finally {
      setTimeout(() => btn.classList.remove("busy"), 3000);
    }
  }

  // -- search ---------------------------------------------------------------

  _setupSearch() {
    const form = this.shadowRoot.getElementById("search");
    const input = this.shadowRoot.getElementById("q");
    const results = this.shadowRoot.getElementById("results");
    form.onsubmit = (e) => {
      e.preventDefault();
      this._search(input.value);
    };
    input.oninput = () => {
      results.hidden = true;
      if (!input.value.trim() && this._planReady) this._planCard.clearPlace?.(); // also the × of the field
    };
    const move = (e, from) => {
      const items = [...results.querySelectorAll("button")];
      if (e.key === "Escape") {
        results.hidden = true;
        input.focus();
      } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (results.hidden || !items.length) return;
        e.preventDefault();
        const i = items.indexOf(from) + (e.key === "ArrowDown" ? 1 : -1);
        (i < 0 ? input : items[Math.min(i, items.length - 1)]).focus();
      }
    };
    input.onkeydown = (e) => move(e, input);
    results.onkeydown = (e) => move(e, e.target);
    // A tap anywhere else closes the result list.
    this.shadowRoot.addEventListener("pointerdown", (e) => {
      if (!form.contains(e.target)) results.hidden = true;
    });
  }

  async _search(text) {
    text = text.trim();
    const form = this.shadowRoot.getElementById("search");
    if (!text || form.classList.contains("busy")) return;
    const coords = parseCoords(text);
    if (coords) {
      this._showResults(null);
      this._showPlace(coords);
      return;
    }
    form.classList.add("busy");
    try {
      const places = await geocode(text, this._hass);
      if (places.length === 1) {
        this._showResults(null);
        this._showPlace(places[0]);
      } else {
        this._showResults(places);
      }
    } catch (err) {
      console.error("dji-flightlog-panel search:", err);
      this._showResults([], `Suche fehlgeschlagen: ${err.message || err}`);
    } finally {
      form.classList.remove("busy");
    }
  }

  _showResults(places, msg = null) {
    const box = this.shadowRoot.getElementById("results");
    if (!places) {
      box.hidden = true;
      return;
    }
    box.innerHTML = places.length
      ? places.map((p, i) => `<button type="button" data-i="${i}">${esc(p.label)}</button>`).join("") +
        `<div class="src">Suche: © OpenStreetMap-Mitwirkende (Nominatim)</div>`
      : `<div class="msg">${esc(msg || "Nichts gefunden.")}</div>`;
    for (const b of box.querySelectorAll("button")) {
      b.onclick = () => {
        box.hidden = true;
        this._showPlace(places[Number(b.dataset.i)]);
      };
    }
    box.hidden = false;
  }

  _showPlace(place) {
    if (this._planReady) this._planCard.showPlace?.(place);
    // Phone: the map sits below the filters and may be scrolled out of view.
    if (this._narrow) this.shadowRoot.getElementById("planwrap").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // -- upload ---------------------------------------------------------------

  _setupDrop() {
    const drop = this.shadowRoot.getElementById("drop");
    const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
    this.addEventListener("dragenter", (e) => {
      if (!hasFiles(e) || !this._hass?.user?.is_admin) return;
      e.preventDefault();
      drop.hidden = false;
    });
    drop.addEventListener("dragover", (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    });
    drop.addEventListener("dragleave", () => (drop.hidden = true));
    drop.addEventListener("drop", async (e) => {
      e.preventDefault();
      drop.hidden = true;
      this._upload(await collectDropped(e.dataTransfer));
    });
  }

  async _upload(files) {
    if (!files.length || this._uploading) return;
    this._uploading = true;
    const btn = this.shadowRoot.getElementById("upload");
    btn.classList.add("busy");
    const results = [];
    try {
      for (const [i, file] of files.entries()) {
        this._renderUpload({ done: i, total: files.length });
        if (!/\.txt$/i.test(file.name)) {
          results.push({ file: file.name, status: "rejected", reason: /\.dat$/i.test(file.name) ? "dat" : "not_txt" });
          continue;
        }
        try {
          const body = new FormData();
          body.append("file", file, file.name);
          const resp = await this._hass.fetchWithAuth(`/api/${API}/upload`, { method: "POST", body });
          if (!resp.ok) {
            throw new Error(resp.status === 401 ? "nur Administratoren dürfen hochladen" : `HTTP ${resp.status}`);
          }
          results.push(...(await resp.json()).results);
        } catch (err) {
          // Also lands here when a file from the controller cannot be read.
          results.push({ file: file.name, status: "error", message: err.message || String(err) });
        }
      }
    } finally {
      this._uploading = false;
      btn.classList.remove("busy");
    }
    this._renderUpload({ results });
    if (results.some((r) => r.status === "imported")) {
      this._load();
      if (this._cardReady) this._card.updateOptions({});
    }
  }

  _renderUpload(state) {
    const bar = this.shadowRoot.getElementById("upbar");
    if (!state) {
      bar.hidden = true;
      return;
    }
    if (!state.results) {
      bar.hidden = false;
      bar.className = "";
      bar.innerHTML = `<div class="txt">Lade hoch … ${state.done + 1} von ${state.total}</div>`;
      return;
    }
    const count = (st) => state.results.filter((r) => r.status === st).length;
    const imported = count("imported");
    const dupes = count("duplicate");
    const problems = state.results.filter((r) => r.status !== "imported" && r.status !== "duplicate");
    const parts = [];
    if (imported) parts.push(`${imported} ${imported === 1 ? "neuer Flug" : "neue Flüge"}`);
    if (dupes) parts.push(`${dupes} schon vorhanden`);
    if (problems.length) parts.push(`${problems.length} nicht importiert`);
    bar.hidden = false;
    bar.className = problems.length ? "problem" : "";
    bar.innerHTML = `
      <div class="txt">
        <div>${esc(parts.join(" · ") || "Keine Dateien")}</div>
        ${
          problems.length
            ? `<ul>${problems
                .slice(0, 20)
                .map((r) => `<li>${esc(r.file)}: ${esc(uploadProblem(r))}</li>`)
                .join("")}${problems.length > 20 ? `<li>… und ${problems.length - 20} weitere</li>` : ""}</ul>`
            : ""
        }
      </div>
      <button title="Schließen">${svg(
        "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z",
      )}</button>`;
    bar.querySelector("button").onclick = () => this._renderUpload(null);
  }

  _renderNote() {
    const el = this.shadowRoot.getElementById("note");
    const flights = this._data?.flights || [];
    const noTrack = flights.filter((f) => f.status === "header_only").length;
    const unsupported = this._hass?.states["sensor.dji_flight_log_unsupported_files"];
    const msgs = [];
    if (noTrack) {
      msgs.push(
        `${noTrack} ${noTrack === 1 ? "Flug hat" : "Flüge haben"} keinen GPS-Track: die Logs sind verschlüsselt. ` +
          `Trage den DJI-API-Key in den Optionen der Integration ein, dann werden sie beim nächsten Scan nachgeladen.`,
      );
    }
    if (unsupported && Number(unsupported.state) > 0) {
      msgs.push(`${unsupported.state} Datei(en) im Log-Ordner sind keine DJI-Fly-Flugaufzeichnungen.`);
    }
    for (const acc of this._data?.media?.accounts || []) {
      if (!acc.ok) {
        msgs.push(
          `Abgleich der Aufnahmen für ${acc.title} (Ordner „${acc.folder}“) fehlgeschlagen. Details stehen im Protokoll; ` +
            (acc.source === "onedrive"
              ? `bei abgelaufener Anmeldung bietet Home Assistant unter Einstellungen → Geräte & Dienste eine neue Anmeldung an.`
              : `ist der Ordner bzw. Netzwerkspeicher eingebunden (Einstellungen → System → Speicher)?`),
        );
      }
    }
    el.hidden = msgs.length === 0;
    el.className = msgs.length ? "note" : "";
    el.innerHTML = msgs.map((m) => `<div>${esc(m)}</div>`).join("");
  }

  /** "Vor dem nächsten Flug": problems from each aircraft's and battery's latest flight. */
  async _renderAttention() {
    const box = this.shadowRoot.getElementById("attention");
    const items = this._data?.attention || [];
    box.hidden = !items.length;
    if (!items.length) {
      box.innerHTML = "";
      return;
    }
    const labels = await loadCardModule();
    const icon = {
      critical: "M13,14H11V9H13M13,18H11V16H13M1,21H23L12,2L1,21Z",
      warning: "M13,14H11V9H13M13,18H11V16H13M1,21H23L12,2L1,21Z",
      info: "M13,9H11V7H13M13,17H11V11H13M12,2A10,10 0 0,0 2,12A10,10 0 0,0 12,22A10,10 0 0,0 22,12A10,10 0 0,0 12,2Z",
    };
    box.innerHTML = `
      <div class="ahead"><span>Vor dem nächsten Flug</span>${
        items.length > 1 ? `<button class="done" data-all>Alle erledigt</button>` : ""
      }</div>
      ${items
        .map(
          (i) => `
        <div class="item ${esc(i.level)}" data-key="${esc(i.key)}">
          <div class="ic">${svg(icon[i.level] || icon.info)}</div>
          <div class="txt">
            <div>${esc(attentionText(i, labels))}</div>
            <div class="sub">${esc(i.aircraft_name || "DJI")} · Flug ${esc(
              fmtDate(i.start_time, { dateStyle: "short", timeStyle: "short" }),
            )} · <a data-flight="${esc(i.flight_id)}">Details</a></div>
          </div>
          <button class="done" title="Als erledigt markieren: bleibt ausgeblendet, bis ein neuer Flug das Problem wieder meldet">Erledigt</button>
        </div>`,
        )
        .join("")}`;
    for (const a of box.querySelectorAll("a[data-flight]")) a.onclick = () => this._openDetails(a.dataset.flight);
    for (const b of box.querySelectorAll(".item button.done")) b.onclick = () => this._dismiss([b.closest(".item").dataset.key]);
    const all = box.querySelector("button[data-all]");
    if (all) all.onclick = () => this._dismiss(items.map((i) => i.key));
  }

  async _dismiss(keys) {
    try {
      await this._hass.callApi("POST", `${API}/attention/dismiss`, { keys });
    } catch (err) {
      console.error("dji-flightlog-panel dismiss:", err);
      return;
    }
    this._load();
  }

  _renderStats() {
    const flights = this._data?.flights || [];
    const sum = (key) => flights.reduce((a, f) => a + (f[key] || 0), 0);
    const max = (key) => flights.reduce((a, f) => Math.max(a, f[key] || 0), 0);
    const tiles = [
      ["Flüge", flights.length],
      ["Flugzeit", fmtDur(sum("duration_s"))],
      ["Strecke", fmtDist(sum("distance_m"))],
      ["Max. Höhe", `${Math.round(max("max_height_m"))} m`],
      ["Max. Speed", `${(max("max_h_speed_ms") * 3.6).toFixed(1)} km/h`],
      ["Letzter Flug", flights.length ? fmtDate(flights[0].start_time, { dateStyle: "short", timeStyle: "short" }) : "–"],
    ];
    if (this._data?.media?.connected) {
      tiles.push(["Aufnahmen", flights.reduce((a, f) => a + (f.media?.length || 0), 0)]);
    }
    this.shadowRoot.getElementById("stats").innerHTML = tiles
      .map(([k, v]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`)
      .join("");

    // Aircraft filter is only useful with more than one drone.
    const aircraft = Object.values(this._data?.aircraft || {});
    const label = this.shadowRoot.getElementById("aclabel");
    const select = this.shadowRoot.getElementById("aircraft");
    label.hidden = aircraft.length < 2;
    if (aircraft.length >= 2 && select.options.length !== aircraft.length + 1) {
      select.innerHTML =
        `<option value="">Alle</option>` +
        aircraft.map((a) => `<option value="${esc(a.sn)}">${esc(a.name)}</option>`).join("");
      select.value = this._filters.aircraft;
    }
  }

  _renderList() {
    if (!this._data) return; // flights not loaded yet
    const list = this.shadowRoot.getElementById("list");
    const flights = this._data?.flights || [];
    if (!flights.length) {
      list.innerHTML = `<div class="empty">Keine Flüge im gewählten Zeitraum.</div>`;
      return;
    }
    const mediaConnected = !!this._data?.media?.connected;
    let html = "";
    let day = null;
    for (const f of flights) {
      const d = fmtDay(f.start_time);
      if (d !== day) {
        day = d;
        html += `<div class="day">${esc(d)}</div>`;
      }
      const time = fmtDate(f.start_time, { timeStyle: "short" });
      const selected = this._selected === f.flight_id;
      const media = f.media || [];
      // The log knows whether the camera recorded; say so if nothing matched.
      const missing =
        mediaConnected && !media.length && (f.video_time_s > 0 || f.photo_num > 0)
          ? `<div class="hint">Laut Log aufgenommen, keine Aufnahme gefunden</div>`
          : "";
      html += `
        <div class="row${selected ? " sel" : ""}" data-id="${esc(f.flight_id)}">
          <div class="dot" style="background:${esc(colorFor(f, flights))}"></div>
          <div class="main">
            <div class="t">${esc(time)} · ${esc(fmtDur(f.duration_s))} · ${esc(fmtDist(f.distance_m))}</div>
            <div class="d">${esc(f.aircraft_name || "DJI")} · max ${Math.round(f.max_height_m || 0)} m${
              f.city ? ` · ${esc(f.city)}` : ""
            }</div>
            ${f.status === "header_only" ? `<div class="warn">kein GPS-Track</div>` : ""}
            ${
              f.incident === "critical" || f.incident === "warning"
                ? `<div class="warn${f.incident === "critical" ? " crit" : ""}" title="${esc((f.incident_actions || []).join(", "))}">${
                    f.incident === "critical" ? "Kritischer Vorfall" : "Warnung"
                  }</div>`
                : ""
            }
            ${f.sd_full ? `<div class="warn">SD-Karte voll</div>` : ""}
            ${missing}
          </div>
          ${media.length ? `<div class="badge" title="${media.length} Aufnahme(n)">${svg(ICON_VIDEO)}${media.length}</div>` : ""}
          <button class="act det" title="Details zum Flug">${svg(
            "M16,11.78L20.24,4.45L21.97,5.45L16.74,14.5L10.23,10.75L5.46,19H22V21H2V3H4V17.54L9.5,8L16,11.78Z",
          )}</button>
        </div>
        ${selected && media.length ? this._mediaHtml(f) : ""}`;
    }
    list.innerHTML = html;
    for (const img of list.querySelectorAll(".media img")) {
      img.onerror = () => img.remove(); // leaves the kind label as placeholder
    }
    for (const el of list.querySelectorAll(".media .m")) {
      const f = flights.find((x) => x.flight_id === el.dataset.fid);
      const m = f?.media?.[Number(el.dataset.i)];
      el.onclick = (e) => {
        if (e.target.closest("a")) return; // "OneDrive"/"Download" link opens by itself
        if (m) this._openMedia(m);
      };
    }
    for (const row of list.querySelectorAll(".row")) {
      row.onclick = () => {
        const id = row.dataset.id;
        this._selected = this._selected === id ? null : id;
        this._renderList();
        this._card?.focusFlight(this._selected);
      };
      row.querySelector(".det").onclick = (ev) => {
        ev.stopPropagation();
        this._openDetails(row.dataset.id);
      };
    }
  }

  _mediaHtml(f) {
    return `<div class="media">${f.media
      .map(
        (m, i) => `
        <div class="m" data-fid="${esc(f.flight_id)}" data-i="${i}" title="${esc(m.name)}">
          <div class="thumb">
            ${esc(KIND_LABEL[m.kind] || m.kind)}
            ${m.thumb ? `<img src="${esc(m.thumb)}" loading="lazy" alt="">` : ""}
            ${m.kind === "360" ? `<span class="k">360°</span>` : ""}
            ${m.duration_s ? `<span class="p">${m.play ? "▶ " : ""}${esc(fmtDur(m.duration_s))}</span>` : ""}
          </div>
          <div class="cap">
            <span>${esc(fmtClock(m.start))}${m.has_raw ? " · RAW" : ""}</span>
            ${sourceLink(m, true)}
          </div>
        </div>`,
      )
      .join("")}</div>`;
  }

  _openMedia(m) {
    if (!m.play) {
      // 360° original without proxy, raw photo, ...: only the source can show it.
      if (m.web_url) window.open(m.web_url, "_blank", "noopener");
      else if (m.download) window.location.assign(m.download);
      return;
    }
    const dlg = this.shadowRoot.getElementById("player");
    const isPhoto = m.kind === "photo";
    dlg.innerHTML = `
      <div class="box">
        <div class="bar">
          <span class="ttl">${esc(m.name)} · ${esc(fmtDate(m.start))}</span>
          ${sourceLink(m)}
          <button class="close" title="Schließen">${svg(ICON_CLOSE)}</button>
        </div>
        ${isPhoto ? `<img class="full" src="${esc(m.play)}" alt="">` : `<video controls autoplay playsinline preload="metadata" src="${esc(m.play)}"></video>`}
        <div class="hint" id="phint"${m.kind === "360" ? "" : " hidden"}>${
          m.kind === "360"
            ? "360°-Vorschau (Proxy der Kamera, beide Fisheye-Linsen nebeneinander). Das Original lässt sich in DJI Studio / LightCut bearbeiten."
            : ""
        }</div>
      </div>`;
    dlg.hidden = false;
    dlg.onclick = (e) => {
      if (e.target === dlg) this._closePlayer();
    };
    dlg.querySelector(".close").onclick = () => this._closePlayer();
    const video = dlg.querySelector("video");
    if (video) {
      video.onerror = () => {
        const hint = dlg.querySelector("#phint");
        hint.hidden = false;
        hint.textContent =
          "Dieses Video kann der Browser nicht abspielen (vermutlich H.265/HEVC ohne Hardware-Decoder). " + sourceHint(m);
      };
    }
    window.addEventListener("keydown", this._onKey);
  }

  _closePlayer() {
    const dlg = this.shadowRoot?.getElementById("player");
    if (dlg && !dlg.hidden) {
      dlg.hidden = true;
      dlg.innerHTML = ""; // stops the video download
    }
    if (this._onKey) window.removeEventListener("keydown", this._onKey);
  }
}

const SD_STATES = {
  NO_CARD: "keine SD-Karte eingelegt",
  INVALID_CARD: "Karte ungültig",
  WRITE_PROTECTED: "Karte schreibgeschützt",
  UNFORMATTED: "Karte nicht formatiert",
  ILLEGAL_FILE_SYS: "falsches Dateisystem",
  LOW_SPEED: "Karte zu langsam für die Aufnahme",
  INDEX_MAX: "maximale Dateianzahl erreicht",
  SUGGEST_FORMAT: "Formatieren empfohlen",
  REPAIRING: "Karte wurde repariert",
};
const num = (v, digits = 0) =>
  Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });

function attentionText(i, labels) {
  const bat = `Akku …${String(i.battery_sn || "").slice(-4)}`;
  switch (i.code) {
    case "incident": {
      const what = (i.actions || []).map((a) => labels?.actionLabel?.(a) || a).join(", ");
      return `${i.level === "critical" ? "Kritischer Vorfall" : "Warnung"} beim letzten Flug: ${what}.`;
    }
    case "sd_full":
      return "SD-Karte war voll: leeren oder tauschen, sonst wird beim nächsten Flug nichts aufgezeichnet.";
    case "sd_low":
      return i.video_left_s
        ? `SD-Karte fast voll: noch etwa ${Math.floor(i.video_left_s / 60)} min Video (${num(i.free_mb / 1024, 1)} GB frei).`
        : `SD-Karte fast voll: ${num(i.free_mb / 1024, 1)} GB frei.`;
    case "sd_problem":
      return `SD-Karte: ${(i.states || []).map((st) => SD_STATES[st] || st).join(", ")}. Karte prüfen oder in der Drohne formatieren.`;
    case "battery_hot":
      return `${bat} wurde ${num(i.temp_c, 1)} °C heiß. Vor dem Laden abkühlen lassen.`;
    case "battery_deep_discharge":
      return `${bat}: eine Zelle fiel auf ${num(i.cell_min_v, 2)} V (tiefentladen). Bald laden und beim nächsten Flug früher landen.`;
    case "battery_cells":
      return `${bat}: die Zellen lagen bis ${num(i.cell_dev_v, 3)} V auseinander. Beim nächsten Laden beobachten.`;
    case "battery_worn": {
      const parts = [];
      if (i.capacity_pct != null) parts.push(`Kapazität ${num(i.capacity_pct)} %`);
      if (i.life_pct != null) parts.push(`Lebensdauer ${num(i.life_pct)} %`);
      return `${bat} lässt nach: ${parts.join(", ")}.`;
    }
  }
  return i.code;
}

/** Let a map card fill its flex area instead of using the card's fixed height. */
function fillPanel(card) {
  const mapEl = card.shadowRoot?.getElementById("map");
  const haCard = card.shadowRoot?.querySelector("ha-card");
  if (!mapEl || !haCard) return;
  mapEl.style.height = "100%";
  haCard.style.height = "100%";
  haCard.style.display = "flex";
  haCard.style.flexDirection = "column";
  mapEl.style.flex = "1 1 auto";
}

const ICON_VIDEO = "M17,10.5V7A1,1 0 0,0 16,6H4A1,1 0 0,0 3,7V17A1,1 0 0,0 4,18H16A1,1 0 0,0 17,17V13.5L21,17.5V6.5L17,10.5Z";
const ICON_CLOSE = "M19,6.41L17.59,5L12,10.59L6.41,5L5,6.41L10.59,12L5,17.59L6.41,19L12,13.41L17.59,19L19,17.59L13.41,12L19,6.41Z";

// Same palette the card uses, so list dots match the tracks.
const PALETTE = [
  "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
  "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
];
function colorFor(flight, flights) {
  const sns = [...new Set(flights.map((f) => f.aircraft_sn || "?"))];
  if (sns.length <= 1) {
    return PALETTE[flights.findIndex((f) => f.flight_id === flight.flight_id) % PALETTE.length];
  }
  return PALETTE[sns.indexOf(flight.aircraft_sn || "?") % PALETTE.length];
}

// Coordinates typed or pasted into the search: "48.13743, 11.57549",
// "48,13743 11,57549", "N 48.13743 E 11.57549", "48°08'14.7"N 11°34'31.8"E"
// (Google Maps) or a map link containing "@48.13743,11.57549". Returns
// { lat, lon }, or null for anything else, which then goes to the geocoder.
function parseCoords(text) {
  const link = text.match(/[@=:](-?\d{1,2}\.\d+),\s*(-?\d{1,3}\.\d+)/);
  if (link) return checkCoords(Number(link[1]), Number(link[2]));
  const s = text.trim().toUpperCase().replace(/[′’‘´]/g, "'").replace(/[″”“]|''/g, '"');
  const tokens = s.match(/[NSEWO]|[-+]?\d+(?:[.,]\d+)?|[°'"]|[\s,;/]+|./g) || [];
  const parts = [];
  let hemi = null; // a leading N/S/E/W/O, for the next number
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    const cur = parts[parts.length - 1];
    if (/^[\s,;/]+$/.test(t) || /^[°'"]$/.test(t)) continue; // units are read with their number
    if (/^[NSEWO]$/.test(t)) {
      if (hemi) return null;
      if (cur && !cur.hemi) cur.hemi = t; // trailing: 48.1N
      else hemi = t; // leading: N 48.1
      continue;
    }
    if (!/\d/.test(t)) return null; // letters etc.: a place name
    const n = Math.abs(Number(t.replace(",", ".")));
    let j = i + 1;
    while (/^\s+$/.test(tokens[j] || "")) j++;
    const unit = { "'": 1, '"': 2 }[tokens[j]];
    if (unit) {
      // Minutes or seconds of the current degree value.
      if (!cur || !cur.deg || cur.vals.length !== unit) return null;
      cur.vals.push(n);
    } else {
      parts.push({ hemi, neg: t.startsWith("-"), deg: tokens[j] === "°", vals: [n] });
      hemi = null;
    }
  }
  if (hemi || parts.length !== 2) return null;
  const value = (p) => {
    const [d, m = 0, sec = 0] = p.vals;
    if (m >= 60 || sec >= 60) return NaN;
    const v = d + m / 60 + sec / 3600;
    return p.neg || p.hemi === "S" || p.hemi === "W" ? -v : v;
  };
  const isLat = (p) => p.hemi === "N" || p.hemi === "S";
  const isLon = (p) => !!p.hemi && !isLat(p);
  let [a, b] = parts;
  if (isLon(a) || isLat(b)) [a, b] = [b, a]; // "E 11.5 N 48.1"
  if (isLon(a) || isLat(b)) return null; // two latitudes or two longitudes
  return checkCoords(value(a), value(b));
}

function checkCoords(lat, lon) {
  return Number.isFinite(lat) && Number.isFinite(lon) && Math.abs(lat) <= 90 && Math.abs(lon) <= 180
    ? { lat, lon }
    : null;
}

// OpenStreetMap's geocoder, queried from the browser like the DIPUL zones.
// Its usage policy allows no search-as-you-type, so this only runs on Enter.
const NOMINATIM = "https://nominatim.openstreetmap.org/search";

async function geocode(text, hass) {
  const base = { format: "jsonv2", limit: "5" };
  if (hass?.language) base["accept-language"] = hass.language;
  const queries = [];
  // A bare postcode also matches house numbers and postcodes abroad, so ask
  // for it as a postcode in HA's country first.
  if (/^\d{4,5}$/.test(text)) {
    queries.push({ postalcode: text, countrycodes: (hass?.config?.country || "de").toLowerCase() });
  }
  queries.push({ q: text });
  for (const q of queries) {
    const res = await fetch(`${NOMINATIM}?${new URLSearchParams({ ...base, ...q })}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const hits = await res.json();
    if (hits.length) return hits.map(toPlace);
  }
  return [];
}

function toPlace(hit) {
  const parts = String(hit.display_name || "").split(", ");
  // Postcodes and house numbers make poor spot names: "80331 Altstadt-Lehel".
  const name = hit.name && !/^\d+$/.test(hit.name) ? hit.name : parts.slice(0, 2).join(" ");
  return {
    lat: Number(hit.lat),
    lon: Number(hit.lon),
    label: hit.display_name,
    name,
    bbox: hit.boundingbox?.map(Number) || null, // [south, north, west, east]
  };
}

function uploadProblem(r) {
  switch (r.reason) {
    case "dat":
      return "DAT-Dateien werden nicht unterstützt, nur DJIFlightRecord_*.txt aus DJI Fly";
    case "not_txt":
      return "keine .txt-Datei, nur DJIFlightRecord_*.txt aus DJI Fly";
    case "too_large":
      return "zu groß für eine Flugaufzeichnung";
    case "support_bundle":
      return "DJI-Support-Paket (verschlüsselt), keine Flugaufzeichnung";
    case "fc_dat":
      return "Flugcontroller-DAT der Drohne, nicht lesbar";
  }
  switch (r.status) {
    case "failed":
      return "konnte nicht gelesen werden";
    case "retry":
      return "DJI-Schlüssel nicht abrufbar, wird beim nächsten Scan erneut versucht";
    case "error":
      return `Fehler: ${r.message}`;
  }
  return r.status;
}

// Files (and, recursively, the .txt files in dropped folders). The items must
// be read synchronously: the DataTransfer is emptied once the handler yields.
async function collectDropped(dt) {
  if (!dt.items) return [...dt.files];
  const files = [];
  const dirs = [];
  for (const item of dt.items) {
    if (item.kind !== "file") continue;
    const entry = item.webkitGetAsEntry?.();
    if (entry?.isDirectory) {
      dirs.push(entry);
    } else {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  for (const dir of dirs) files.push(...(await readDir(dir)));
  return files;
}

async function readDir(dir) {
  const out = [];
  const reader = dir.createReader();
  for (;;) {
    // readEntries hands out at most ~100 entries per call.
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) break;
    for (const entry of batch) {
      if (entry.name.startsWith(".")) continue;
      if (entry.isDirectory) out.push(...(await readDir(entry)));
      else if (/\.txt$/i.test(entry.name)) out.push(await new Promise((res, rej) => entry.file(res, rej)));
    }
  }
  return out;
}

function svg(path) {
  return `<svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor"><path d="${path}"/></svg>`;
}

if (!customElements.get("dji-flightlog-panel")) {
  customElements.define("dji-flightlog-panel", DjiFlightLogPanel);
}
