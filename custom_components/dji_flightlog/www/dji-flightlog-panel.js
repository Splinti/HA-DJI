/*
 * dji-flightlog-panel
 *
 * Full-page Home Assistant panel (sidebar entry) for the dji_flightlog
 * integration: statistics, filters, a large map, a clickable flight list and
 * the saved spots ("Ort merken" picks a new one on the map, with the DIPUL
 * geo zones shown). ``?spot=<id>`` in the URL opens that spot. Flight records
 * can be uploaded with the upload button or by dropping files / folders onto
 * the page (admins only).
 *
 * Registered by the integration via panel_custom; the map itself is the
 * dji-flight-map-card element, which this module loads on demand.
 */

const STATIC = "/dji_flightlog_static";
const API = "dji_flightlog";

let cardPromise = null;
function loadCard() {
  if (customElements.get("dji-flight-map-card")) return Promise.resolve();
  // Same ?v= as this module (set by the integration), so an update is not
  // served a stale card from the browser cache.
  cardPromise ??= import(`${STATIC}/dji-flight-map-card.js${new URL(import.meta.url).search}`);
  return cardPromise;
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
const esc = (t) =>
  String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

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
    this._tab = "flights";
    this._planning = false;
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
    // Only once the card is upgraded and configured (see _setupCard).
    if (this._cardReady) this._card.hass = hass;
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
        #list { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
        .tabs { display: flex; flex: 0 0 auto; border-bottom: 1px solid var(--divider-color, #e0e0e0); }
        .tabs button {
          flex: 1; background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer;
          font: inherit; font-size: 14px; padding: 10px; color: var(--secondary-text-color);
        }
        .tabs button.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
        /* Phone: the page scrolls, the map gets a fixed share of the screen. */
        :host(.narrow) { height: auto; min-height: 100dvh; }
        .layout.narrow .body { flex-direction: column; }
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
        .row .pin { flex: 0 0 auto; color: #ff9800; line-height: 0; }
        .row .act { flex: 0 0 auto; display: inline-flex; color: var(--secondary-text-color); padding: 6px; border-radius: 50%; background: none; border: none; cursor: pointer; line-height: 0; }
        .row .act:hover { background: var(--divider-color, #e0e0e0); color: var(--primary-text-color); }
        .row a.act { color: var(--primary-color); }
        .hint { padding: 12px 16px; font-size: 13px; color: var(--secondary-text-color); }
        .empty { padding: 24px 16px; color: var(--secondary-text-color); text-align: center; }
        .note {
          margin: 0 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px;
          background: var(--warning-color, #ffa600); color: #000;
        }
        .note a { color: inherit; }

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
          <button id="plan" title="Ort merken: Punkt auf der Karte wählen, DIPUL-Zonen werden eingeblendet">${svg(
            "M20,14H18V11H15V9H18V6H20V9H23V11H20V14M12,2C15.86,2 19,5.14 19,9C19,14.25 12,22 12,22C12,22 5,14.25 5,9A7,7 0 0,1 12,2M12,6.5A2.5,2.5 0 0,0 9.5,9A2.5,2.5 0 0,0 12,11.5A2.5,2.5 0 0,0 14.5,9A2.5,2.5 0 0,0 12,6.5Z",
          )}</button>
          <button id="upload" title="Flugaufzeichnungen hochladen (DJIFlightRecord_*.txt), oder Dateien auf die Seite ziehen" hidden>${svg(
            "M9,16V10H5L12,3L19,10H15V16H9M5,20V18H19V20H5Z",
          )}</button>
          <input type="file" id="file" accept=".txt" multiple hidden>
          <button id="scan" title="Log-Ordner jetzt scannen">${svg(
            "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
          )}</button>
        </header>

        <div id="upbar" hidden></div>
        <div id="note" hidden></div>

        <div class="stats" id="stats"></div>

        <div class="filters">
          <form class="search" id="search" role="search">
            <input type="search" id="q" placeholder="PLZ, Ort, Adresse oder Koordinaten" autocomplete="off"
              title="z. B. 80331, Marienplatz München, 48.13743, 11.57549 oder 48°08'14.7&quot;N 11°34'31.8&quot;E">
            <div id="results" hidden></div>
          </form>
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
          <div class="mapwrap"><dji-flight-map-card></dji-flight-map-card></div>
          <aside>
            <div class="tabs">
              <button data-tab="flights">Flüge</button>
              <button data-tab="spots">Orte</button>
            </div>
            <div id="list"></div>
          </aside>
        </div>
      </div>
      <div id="drop" hidden><div>Flugaufzeichnungen hier ablegen<small>DJIFlightRecord_*.txt oder der Ordner FlightRecord</small></div></div>`;

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
    this.shadowRoot.getElementById("plan").onclick = () => this._setPlanning(!this._planning);
    for (const tab of this.shadowRoot.querySelectorAll(".tabs button")) {
      tab.onclick = () => this._setTab(tab.dataset.tab);
    }
    // Fired by the map card after a spot was saved, re-checked or deleted.
    this.shadowRoot.addEventListener("dji-spots-changed", () => this._loadSpots());
    this._setTab(this._tab);

    this.shadowRoot.querySelector(".layout").classList.toggle("narrow", !!this._narrow);
    this._setupCard();
  }

  get _card() {
    return this.shadowRoot?.querySelector("dji-flight-map-card");
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
      spot_on_click: true,
      dipul: this._filters.dipul,
    });
    // Let the map fill the panel instead of using the card's fixed height.
    const mapEl = card.shadowRoot?.getElementById("map");
    if (mapEl) mapEl.style.height = "100%";
    const haCard = card.shadowRoot?.querySelector("ha-card");
    if (haCard) {
      haCard.style.height = "100%";
      haCard.style.display = "flex";
      haCard.style.flexDirection = "column";
      mapEl.style.flex = "1 1 auto";
    }
    this._cardReady = true;
    if (this._hass) {
      card.hass = this._hass;
      // Force the first load instead of relying on a refresh-entity change.
      card.updateOptions(this._cardFilters());
    }
    if (this._planning) card.setPlanning(true);
    this._openSpotFromUrl();
  }

  _setPlanning(on) {
    this._planning = on;
    this.shadowRoot.getElementById("plan").classList.toggle("active", on);
    this._card?.setPlanning?.(on);
    if (on) this._setTab("spots");
    else this._renderList(); // drop the planning hint
  }

  _setTab(tab) {
    this._tab = tab;
    for (const b of this.shadowRoot.querySelectorAll(".tabs button")) b.classList.toggle("on", b.dataset.tab === tab);
    this._renderList();
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
    const tab = this.shadowRoot.querySelector('.tabs button[data-tab="spots"]');
    if (tab) tab.textContent = this._spots.length ? `Orte (${this._spots.length})` : "Orte";
    if (this._tab === "spots") this._renderList();
    this._openSpotFromUrl();
  }

  _openSpotFromUrl() {
    const url = new URL(location.href);
    const id = url.searchParams.get("spot");
    if (!id || !this._card?.focusSpot) return;
    url.searchParams.delete("spot");
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
    if (this._tab !== "spots") this._setTab("spots");
    this._card.focusSpot(id);
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
    this._card?.reloadSpots();
  }

  _renderSpots() {
    const list = this.shadowRoot.getElementById("list");
    const intro = this._planning
      ? `<div class="hint">Auf die Karte tippen, Namen eingeben, „Merken“. Die DIPUL-Zonen sind nur zur Orientierung: vor dem Flug auf dipul.de prüfen.</div>`
      : "";
    if (!this._spots.length) {
      list.innerHTML =
        intro ||
        `<div class="empty">Noch keine Orte gemerkt.<br>Auf die Karte tippen, um einen Ort zu merken.</div>`;
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
      row.onclick = () => this._card?.focusSpot(spot.id);
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
        this._card?.updateOptions({});
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
      if (!input.value.trim()) this._card?.clearPlace?.(); // also the × of the field
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
    this._card?.showPlace?.(place);
    // Phone: the map sits below the filters and may be scrolled out of view.
    if (this._narrow) this.shadowRoot.querySelector(".mapwrap").scrollIntoView({ block: "nearest", behavior: "smooth" });
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
      this._card?.updateOptions({});
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
    el.hidden = msgs.length === 0;
    el.className = msgs.length ? "note" : "";
    el.innerHTML = msgs.map((m) => `<div>${esc(m)}</div>`).join("");
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
    if (this._tab === "spots") return this._renderSpots();
    if (!this._data) return; // flights not loaded yet
    const list = this.shadowRoot.getElementById("list");
    const flights = this._data?.flights || [];
    if (!flights.length) {
      list.innerHTML = `<div class="empty">Keine Flüge im gewählten Zeitraum.</div>`;
      return;
    }
    let html = "";
    let day = null;
    for (const f of flights) {
      const d = fmtDay(f.start_time);
      if (d !== day) {
        day = d;
        html += `<div class="day">${esc(d)}</div>`;
      }
      const time = fmtDate(f.start_time, { timeStyle: "short" });
      html += `
        <div class="row${this._selected === f.flight_id ? " sel" : ""}" data-id="${esc(f.flight_id)}">
          <div class="dot" style="background:${esc(colorFor(f, flights))}"></div>
          <div class="main">
            <div class="t">${esc(time)} · ${esc(fmtDur(f.duration_s))} · ${esc(fmtDist(f.distance_m))}</div>
            <div class="d">${esc(f.aircraft_name || "DJI")} · max ${Math.round(f.max_height_m || 0)} m${
              f.city ? ` · ${esc(f.city)}` : ""
            }</div>
            ${f.status === "header_only" ? `<div class="warn">kein GPS-Track</div>` : ""}
          </div>
        </div>`;
    }
    list.innerHTML = html;
    for (const row of list.querySelectorAll(".row")) {
      row.onclick = () => {
        const id = row.dataset.id;
        this._selected = this._selected === id ? null : id;
        this._renderList();
        this._card?.focusFlight(this._selected);
      };
    }
  }
}

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
