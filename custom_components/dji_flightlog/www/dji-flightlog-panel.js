/*
 * dji-flightlog-panel
 *
 * Full-page Home Assistant panel (sidebar entry) for the dji_flightlog
 * integration: statistics, filters, a large map and a clickable flight list.
 *
 * Registered by the integration via panel_custom; the map itself is the
 * dji-flight-map-card element, which this module loads on demand.
 */

const STATIC = "/dji_flightlog_static";
const API = "dji_flightlog";

let cardPromise = null;
function loadCard() {
  if (customElements.get("dji-flight-map-card")) return Promise.resolve();
  cardPromise ??= import(`${STATIC}/dji-flight-map-card.js`);
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
    this._filters = { days: "0", aircraft: "", heatmap: false };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (!this._rendered) this._render();
    const card = this._card;
    if (card) card.hass = hass;
    // Reload whenever the integration reports a new import.
    const ent = hass.states["sensor.dji_flight_log_last_import"];
    const key = ent ? ent.state : "none";
    if (first || key !== this._lastKey) {
      this._lastKey = key;
      this._load();
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

  set route(_route) {}

  _fireMenu() {
    this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
  }

  _render() {
    this._rendered = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          height: 100%;
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
        header button.busy svg { animation: spin 1s linear infinite; }
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

        .body { flex: 1 1 auto; display: flex; gap: 12px; padding: 12px 16px 16px; min-height: 0; box-sizing: border-box; }
        .mapwrap { flex: 1 1 auto; min-width: 0; min-height: 260px; display: flex; }
        .mapwrap dji-flight-map-card { flex: 1 1 auto; display: block; }
        aside {
          flex: 0 0 320px; overflow-y: auto; background: var(--card-background-color, #fff);
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
        }
        .layout.narrow .body { flex-direction: column; }
        .layout.narrow aside { flex: 0 0 auto; max-height: 45vh; }

        .day { padding: 10px 16px 4px; font-size: 12px; font-weight: 500; color: var(--secondary-text-color); position: sticky; top: 0; background: var(--card-background-color, #fff); }
        .row { display: flex; align-items: center; gap: 10px; padding: 10px 16px; cursor: pointer; border-left: 4px solid transparent; }
        .row:hover { background: var(--secondary-background-color, #f2f2f2); }
        .row.sel { background: var(--secondary-background-color, #f2f2f2); border-left-color: var(--primary-color); }
        .row .dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; }
        .row .main { flex: 1; min-width: 0; }
        .row .t { font-size: 14px; }
        .row .d { font-size: 12px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .row .warn { font-size: 11px; color: var(--warning-color, #ffa600); }
        .empty { padding: 24px 16px; color: var(--secondary-text-color); text-align: center; }
        .note {
          margin: 0 16px; padding: 10px 14px; border-radius: 8px; font-size: 13px;
          background: var(--warning-color, #ffa600); color: #000;
        }
        .note a { color: inherit; }
      </style>
      <div class="layout page">
        <header>
          <button id="menu" title="Menü">${svg("M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z")}</button>
          <div class="title">Drohnenflüge</div>
          <button id="scan" title="Log-Ordner jetzt scannen">${svg(
            "M17.65,6.35C16.2,4.9 14.21,4 12,4A8,8 0 0,0 4,12A8,8 0 0,0 12,20C15.73,20 18.84,17.45 19.73,14H17.65C16.83,16.33 14.61,18 12,18A6,6 0 0,1 6,12A6,6 0 0,1 12,6C13.66,6 15.14,6.69 16.22,7.78L13,11H20V4L17.65,6.35Z",
          )}</button>
        </header>

        <div id="note" hidden></div>

        <div class="stats" id="stats"></div>

        <div class="filters">
          <label>Zeitraum
            <select id="range">${RANGES.map((r) => `<option value="${r.value}">${r.label}</option>`).join("")}</select>
          </label>
          <label id="aclabel" hidden>Drohne
            <select id="aircraft"><option value="">Alle</option></select>
          </label>
          <label><input type="checkbox" id="heat"> Heatmap</label>
        </div>

        <div class="body">
          <div class="mapwrap"><dji-flight-map-card></dji-flight-map-card></div>
          <aside id="list"></aside>
        </div>
      </div>`;

    this.shadowRoot.getElementById("menu").onclick = () => this._fireMenu();
    this.shadowRoot.getElementById("scan").onclick = () => this._scan();
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
    card.setConfig({
      title: "",
      mode: "all",
      height: 100, // overridden by CSS; the card fills the flex area
      heatmap: this._filters.heatmap,
      scan_button: false,
      refresh_seconds: 0, // the panel drives reloads
      fit: true,
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
    if (this._hass) {
      card.hass = this._hass;
      // Force the first load instead of relying on a refresh-entity change.
      card.updateOptions(this._cardFilters());
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
    if (!this._hass || this._loading) return;
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

function svg(path) {
  return `<svg viewBox="0 0 24 24" width="24" height="24" fill="currentColor"><path d="${path}"/></svg>`;
}

if (!customElements.get("dji-flightlog-panel")) {
  customElements.define("dji-flightlog-panel", DjiFlightLogPanel);
}
