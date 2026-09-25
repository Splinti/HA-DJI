/*
 * dji-360-view
 *
 * 360° view for the players of the dji_flightlog panel and flight view: draws
 * a <video> onto the inside of a sphere with WebGL, to look around by dragging
 * (mouse, finger, pen) and zoom with the mouse wheel or two fingers. No
 * library: one triangle covers the canvas, and the fragment shader turns
 * every pixel into a ray and looks that up in the video frame.
 *
 * Two projections:
 *   equirect  an equirectangular video (`<name>_360.mp4`, stitched on the PC)
 *   dfisheye  the camera's proxy (.LRF) as is: two fisheye circles side by
 *             side, the left lens pointing up (sky), the right one down
 *             (ground). Each sees more than 180°, so the circles overlap
 *             around the horizon and are crossfaded there. See AVATA360_LENS.
 *
 * The <video> stays the source of truth (sync with the charts, seeking,
 * errors): it stays in the DOM and keeps decoding, only invisible under the
 * canvas, and own controls (play, position, time) stand in for its native
 * ones. A "360° | Flach" switch shows the raw picture with the native controls.
 *
 *   const view = Dji360View.create(video, container, { projection: "dfisheye" });
 *   if (!view) ...  // no WebGL: the flat video stays as it is
 *   view.destroy(); // before the player is rebuilt
 */

/**
 * Dual-fisheye layout of the Avata 360 proxy, measured on a 1920×960 .LRF.
 *
 * Both lenses are equidistant fisheyes: a ray θ off the lens axis lands
 * f·θ pixels from the circle's centre. The values are pixels of `size`; a
 * proxy of another size (same layout) scales along.
 * Per lens: centre (cx, cy); rot: where "forward" (yaw 0) lies in the circle,
 * degrees clockwise from its top (180: forward is image-down); mirror: false
 * if turning right in the world runs clockwise in the circle, true if
 * counter-clockwise. f and maxTheta may also be set per lens.
 */
export const AVATA360_LENS = {
  size: [1920, 960],
  f: 282.73, // px per radian (≈ 194.5° across 960 px)
  maxTheta: 94, // degrees off the axis that still show the scene; beyond is lens rim / housing
  // Left half, pointing up (sky): image right = world right.
  up: { cx: 479.76, cy: 480.73, rot: 180, mirror: true },
  // Right half, pointing down (ground): mirrored, image right = world left; trimmed by −0.33°.
  down: { cx: 1439.37, cy: 478.63, rot: 179.67, mirror: false },
  // Width of the crossfade band around the horizon, degrees (−2° … +2°; at most the overlap).
  blend: 4,
  // Tilt of the lens pair against the horizon, degrees; turns every ray before
  // the lens lookup (yaw about the vertical, pitch about the side axis, roll about forward).
  // The proxy is not levelled (the drone tilts in flight), so this stays 0.
  rig: { yaw: 0, pitch: 0, roll: 0 },
};

/** Hints under a 360° player, per projection. */
export const HINT_360 = {
  dfisheye: "360°-Vorschau aus dem Proxy der Kamera, im Browser entzerrt (an der Naht am Horizont nicht ganz sauber). Das Original lässt sich in DJI Studio / LightCut bearbeiten.",
  equirect: "360°-Video, auf dem PC entzerrt. Das Original lässt sich in DJI Studio / LightCut bearbeiten.",
};
export const HINT_NO_WEBGL = "Die 360°-Ansicht braucht WebGL; der Browser zeigt daher das Bild, wie es in der Datei steht.";

/** The camera's proxy (.LRF, "_proxy") rather than an original. */
const isProxy = (m) => /\.lrf$/i.test(m.name || "") || /_proxy\.\w+$/i.test(m.name || "") || m.has_original === false;

/**
 * How a recording's video is projected: "equirect", "dfisheye" or null (flat).
 * The backend says so where it can; a 360° recording without the field (older
 * integration) is the proxy. A proxy it could not classify (OneDrive knows
 * nothing about an .LRF without its .OSV) is the dual fisheye if the video is
 * 2:1: `size` ({ width, height }) once its metadata are in.
 */
export function projectionOf(m, size = null) {
  if (!m?.play || m.kind === "photo") return null;
  if (m.projection) return m.projection;
  if (m.projection === undefined && m.kind === "360") return "dfisheye";
  if (m.kind === "video" && isProxy(m) && size?.height && size.width === 2 * size.height) return "dfisheye";
  return null;
}

/** True while projectionOf(m) may still turn into "dfisheye" once the video's size is known. */
export function needsSize(m) {
  return !!m?.play && m.kind === "video" && !m.projection && isProxy(m);
}

const MIN_FOV = 30;
const MAX_FOV = 120;
const DEF_FOV = 90;
const MAX_PIXELS = 4e6; // canvas cap in device pixels (fullscreen on a 4K screen)
const HINT_KEY = "dji_flightlog.v360_dragged"; // set once someone dragged: no more hint
const RAD = Math.PI / 180;

const GL_OPTS = { alpha: true, antialias: false, depth: false, stencil: false, powerPreference: "low-power" };

const VERT = `
attribute vec2 aPos;
varying vec2 vPos;
void main() {
  vPos = aPos;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

// Coordinates: x right, y up, z forward (yaw 0, the middle of an equirect frame).
const FRAG = `
precision highp float;
varying vec2 vPos;         // -1..1 across the canvas
uniform sampler2D uTex;
uniform float uFisheye;    // 0: equirect, 1: dual fisheye
uniform vec2 uTan;         // tan of half the view angle, across and up
uniform mat3 uView;        // camera -> world: yaw and pitch of the view
uniform mat3 uRig;         // world -> lens pair (fisheye only)
uniform vec4 uUp;          // up lens: centre (texture), f (texture units per rad, across and down)
uniform vec4 uDown;        // down lens: the same
uniform vec4 uTurn;        // rot (rad), mirror (-1 / 1): up lens in xy, down lens in zw
uniform vec2 uMax;         // up, down: largest usable angle off the axis (rad)
uniform float uBlend;      // half width of the crossfade band (rad)

const float PI = 3.14159265;

// Direction d -> position in the circle of one lens (axis: 1 up lens, -1 down lens).
// xy: texture position, z: 1 where the lens shows the scene, 0 on its rim or beyond.
vec3 lensUv(vec3 d, float axis, vec4 lens, vec2 turn, float maxTheta) {
  // 1. angle between the ray and the lens axis: 0 in the circle's centre
  float theta = acos(clamp(axis * d.y, -1.0, 1.0));
  // 2. azimuth around the axis (0 forward, +90 deg right), turned and mirrored into the
  //    circle: clockwise from its top (straight along the axis the azimuth does not matter)
  float az = length(d.xz) > 1e-6 ? atan(d.x, d.z) : 0.0;
  float ang = turn.x + turn.y * az;
  // 3. equidistant fisheye: f * theta from the centre, in that direction (texture y runs down)
  vec2 uv = lens.xy + theta * lens.zw * vec2(sin(ang), -cos(ang));
  return vec3(uv, step(theta, maxTheta));
}

void main() {
  vec3 ray = normalize(uView * vec3(vPos * uTan, 1.0));
  vec3 color;
  if (uFisheye < 0.5) {
    // Equirect: longitude across (yaw 0 in the middle), latitude down from the top.
    float lon = atan(ray.x, ray.z);
    float lat = asin(clamp(ray.y, -1.0, 1.0));
    color = texture2D(uTex, vec2(0.5 + lon / (2.0 * PI), 0.5 - lat / PI)).rgb;
  } else {
    vec3 d = uRig * ray;
    vec3 a = lensUv(d, 1.0, uUp, uTurn.xy, uMax.x);
    vec3 b = lensUv(d, -1.0, uDown, uTurn.zw, uMax.y);
    // Share of the up lens: 1 above the band around the horizon, 0 below it.
    float lat = asin(clamp(d.y, -1.0, 1.0));
    float w = uBlend > 0.0 ? smoothstep(-uBlend, uBlend, lat) : step(0.0, lat);
    color = mix(texture2D(uTex, b.xy).rgb * b.z, texture2D(uTex, a.xy).rgb * a.z, w);
  }
  gl_FragColor = vec4(color, 1.0);
}`;

const CSS = `
  .v360 > .v360-canvas {
    position: absolute; inset: 0; width: 100%; height: 100%; display: none;
    touch-action: none; cursor: grab; outline: none;
  }
  .v360.v360-on > .v360-canvas { display: block; }
  .v360 > .v360-canvas.drag { cursor: grabbing; }
  .v360.v360-on.v360-live > video { opacity: 0; }
  .v360:not(.v360-on) > .v360-bar, .v360:not(.v360-on) > .v360-hint { display: none; }
  .v360-bar {
    position: absolute; left: 0; right: 0; bottom: 0; z-index: 2; display: flex; align-items: center; gap: 4px;
    padding: 18px 6px 2px; background: linear-gradient(transparent, rgba(0,0,0,.6)); color: #fff; font-size: 12px;
  }
  .v360-bar button, .v360-switch button { background: none; border: none; color: inherit; cursor: pointer; font: inherit; }
  .v360-bar button { width: 32px; height: 32px; padding: 5px; border-radius: 50%; line-height: 0; flex: none; }
  .v360-bar button:hover { background: rgba(255,255,255,.18); }
  .v360-bar button[hidden] { display: none; }
  .v360-bar svg { width: 22px; height: 22px; fill: currentColor; transition: transform .1s; }
  .v360-seek { flex: 1; min-width: 40px; margin: 0 4px; accent-color: var(--primary-color, #03a9f4); cursor: pointer; }
  .v360-time { white-space: nowrap; font-variant-numeric: tabular-nums; margin-right: 4px; }
  .v360-switch {
    position: absolute; top: 6px; left: 6px; z-index: 2; display: flex; border-radius: 16px; overflow: hidden;
    background: rgba(0,0,0,.55); color: #fff; font-size: 12px;
  }
  .v360-switch button { padding: 7px 10px; line-height: 1; }
  .v360-switch button:hover { background: rgba(255,255,255,.18); }
  .v360-switch button[aria-pressed="true"] { background: var(--primary-color, #03a9f4); }
  .v360-hint {
    position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); z-index: 1; pointer-events: none;
    padding: 8px 14px; border-radius: 18px; background: rgba(0,0,0,.6); color: #fff; font-size: 14px;
    white-space: nowrap; opacity: 0; transition: opacity .4s;
  }
  .v360-hint.show { opacity: 1; }
`;

const ICON_PLAY = "M8,5.14V19.14L19,12.14L8,5.14Z";
const ICON_PAUSE = "M14,19H18V5H14M6,19H10V5H6V19Z";
const ICON_SOUND = "M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z";
const ICON_MUTED = "M3,9H7L12,4V20L7,15H3V9M16.59,12L14,9.41L15.41,8L18,10.59L20.59,8L22,9.41L19.41,12L22,14.59L20.59,16L18,13.41L15.41,16L14,14.59L16.59,12Z";
const ICON_NORTH = "M12,2L4.5,20.29L5.21,21L12,18L18.79,21L19.5,20.29L12,2Z";
const ICON_FULL = "M5,5H10V7H7V10H5V5M14,5H19V10H17V7H14V5M17,14H19V19H14V17H17V14M10,17V19H5V14H7V17H10Z";
const ICON_FULL_EXIT = "M14,14H19V16H16V19H14V14M5,14H10V19H8V16H5V14M8,5H10V10H5V8H8V5M19,8V10H14V5H16V8H19Z";

export class Dji360View {
  /**
   * 360° view of `video` inside `container` (its positioned parent), or null
   * without WebGL; the flat video then stays as it is.
   * Options: projection ("equirect" | "dfisheye"), lens (see AVATA360_LENS),
   * duration (seconds or a function, for seeking before the video's metadata are in).
   */
  static create(video, container, options = {}) {
    const canvas = document.createElement("canvas");
    let gl = null;
    try {
      gl = canvas.getContext("webgl", GL_OPTS) || canvas.getContext("experimental-webgl", GL_OPTS);
    } catch {
      // Blocked by the browser: same as none.
    }
    if (!gl) return null;
    try {
      return new Dji360View(video, container, canvas, gl, options);
    } catch (err) {
      console.warn("dji-360-view:", err);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
      return null;
    }
  }

  constructor(video, container, canvas, gl, { projection = "equirect", lens = AVATA360_LENS, duration = null } = {}) {
    this.video = video;
    this.container = container;
    this.canvas = canvas;
    this.gl = gl;
    this.projection = projection;
    this.lens = lens;
    this.yaw = 0; // degrees, + right
    this.pitch = 0; // degrees, + up
    this.fov = DEF_FOV; // degrees across the longer side
    this.mode = "360";
    this._duration = duration;
    this._controls = video.controls;
    this._abort = new AbortController();
    this._initGl();
    this._build();
    this._wire();
    this.setMode(video.error ? "flat" : "360"); // failed before we came: nothing to dewarp
  }

  /** "360" (dewarped, own controls) or "flat" (the raw picture with the native controls). */
  setMode(mode) {
    this.mode = mode === "flat" ? "flat" : "360";
    const on = this.mode === "360";
    this.container.classList.toggle("v360-on", on);
    this.video.controls = on ? false : this._controls;
    for (const b of this._switch.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.mode === this.mode));
    if (on) {
      this._resize();
      this._frame();
      this._updateBar();
    } else {
      this._cancelFrame();
      this._hint(false);
    }
  }

  /** Swap the lens layout (for trying calibration values in the console). */
  setLens(lens) {
    this.lens = lens;
    this._setLensUniforms();
    this._requestDraw();
  }

  /** Refresh the controls (e.g. once the host learned the duration). */
  update() {
    this._updateBar();
  }

  /** Look forward again at the default zoom. */
  resetView() {
    this.fov = DEF_FOV;
    this._look(0, 0);
  }

  /** Stop all loops and observers, free the WebGL context, give the video its controls back. */
  destroy() {
    if (this._dead) return;
    this._dead = true;
    this._abort.abort();
    this._cancelFrame();
    cancelAnimationFrame(this._drawRaf);
    cancelAnimationFrame(this._inertiaRaf);
    clearTimeout(this._hintTimer);
    this._ro?.disconnect();
    if (this.container.matches(":fullscreen")) document.exitFullscreen?.().catch(() => {});
    this.gl.getExtension("WEBGL_lose_context")?.loseContext();
    for (const n of this._nodes) n.remove();
    this.container.classList.remove("v360", "v360-on", "v360-live");
    this.video.controls = this._controls;
  }

  // -- WebGL ------------------------------------------------------------------

  _initGl() {
    const gl = this.gl;
    const prog = gl.createProgram();
    for (const [type, src] of [
      [gl.VERTEX_SHADER, VERT],
      [gl.FRAGMENT_SHADER, FRAG],
    ]) {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS) && !gl.isContextLost()) throw new Error(gl.getShaderInfoLog(sh));
      gl.attachShader(prog, sh);
    }
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS) && !gl.isContextLost()) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    // One triangle that covers the whole canvas.
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    // Video frames are not a power of two: no mipmaps, no repeat (WebGL 1).
    gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    this._u = {};
    for (const name of ["uFisheye", "uTan", "uView", "uRig", "uUp", "uDown", "uTurn", "uMax", "uBlend"]) {
      this._u[name] = gl.getUniformLocation(prog, name);
    }
    gl.uniform1f(this._u.uFisheye, this.projection === "dfisheye" ? 1 : 0);
    this._hasFrame = false;
    this._setLensUniforms();
  }

  _setLensUniforms() {
    const gl = this.gl;
    const L = { ...AVATA360_LENS, ...this.lens };
    const up = { ...AVATA360_LENS.up, ...L.up };
    const down = { ...AVATA360_LENS.down, ...L.down };
    const [w, h] = L.size;
    const u = this._u;
    // Pixels of `size` -> texture coordinates (0..1, y down: frames are uploaded unflipped).
    const lens = (x) => [x.cx / w, x.cy / h, (x.f ?? L.f) / w, (x.f ?? L.f) / h];
    gl.uniform4f(u.uUp, ...lens(up));
    gl.uniform4f(u.uDown, ...lens(down));
    gl.uniform4f(u.uTurn, up.rot * RAD, up.mirror ? -1 : 1, down.rot * RAD, down.mirror ? -1 : 1);
    const maxUp = (up.maxTheta ?? L.maxTheta) * RAD;
    const maxDown = (down.maxTheta ?? L.maxTheta) * RAD;
    gl.uniform2f(u.uMax, maxUp, maxDown);
    // The band can be no wider than the overlap, else it fades into the black beyond a lens' rim.
    const overlap = Math.min(maxUp, maxDown) - Math.PI / 2;
    gl.uniform1f(u.uBlend, Math.max(0, Math.min((L.blend / 2) * RAD, overlap)));
    const rig = { yaw: 0, pitch: 0, roll: 0, ...L.rig };
    gl.uniformMatrix3fv(u.uRig, false, colMajor(mul(mul(rotY(rig.yaw * RAD), rotX(rig.pitch * RAD)), rotZ(rig.roll * RAD))));
  }

  /** Copy the current video frame into the texture; false if there is none (yet). */
  _upload() {
    const v = this.video;
    const gl = this.gl;
    if (this.mode !== "360" || v.readyState < 2 || !v.videoWidth || gl.isContextLost()) return false;
    try {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, v);
    } catch (err) {
      // A video from another origin without CORS cannot be read: show it flat.
      console.warn("dji-360-view:", err);
      this.setMode("flat");
      return false;
    }
    if (!this._hasFrame) {
      this._hasFrame = true;
      this.container.classList.add("v360-live");
      this._hint(true);
    }
    return true;
  }

  _draw() {
    this._drawRaf = null;
    const gl = this.gl;
    const { width: w, height: h } = this.canvas;
    if (this.mode !== "360" || !w || !h || gl.isContextLost()) return;
    gl.viewport(0, 0, w, h);
    if (!this._hasFrame) {
      // Nothing decoded yet: transparent, the poster underneath shows.
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      return;
    }
    // The zoom's angle spans the longer side.
    const t = Math.tan((this.fov / 2) * RAD);
    gl.uniform2f(this._u.uTan, w >= h ? t : (t * w) / h, w >= h ? (t * h) / w : t);
    gl.uniformMatrix3fv(this._u.uView, false, colMajor(mul(rotY(this.yaw * RAD), rotX(this.pitch * RAD))));
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  /** Redraw on the next repaint (view changed; the texture stays). */
  _requestDraw() {
    this._drawRaf ??= requestAnimationFrame(() => this._draw());
  }

  /**
   * Upload and draw the frame shown now, and again once the next one is
   * presented: after loading or seeking the new frame may not be decoded yet
   * (the upload would copy black). While playing that goes on frame by frame.
   */
  _frame() {
    this._upload();
    this._draw();
    this._armFrame();
  }

  // Once per decoded frame; without requestVideoFrameCallback (older Firefox) once per repaint.
  _armFrame() {
    if (this._frameReq != null || this.mode !== "360" || this._dead) return;
    const v = this.video;
    const next = () => {
      this._frameReq = null;
      if (this.mode !== "360" || this._dead) return;
      this._upload();
      this._draw();
      this._updateBar();
      if (!v.paused && !v.ended) this._armFrame();
    };
    this._frameVfc = !!v.requestVideoFrameCallback;
    this._frameReq = this._frameVfc ? v.requestVideoFrameCallback(next) : requestAnimationFrame(next);
  }

  _cancelFrame() {
    if (this._frameReq == null) return;
    if (this._frameVfc) this.video.cancelVideoFrameCallback?.(this._frameReq);
    else cancelAnimationFrame(this._frameReq);
    this._frameReq = null;
  }

  /** Canvas at the container's size in device pixels (capped); resizing clears it, so draw right away. */
  _resize() {
    const c = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    let w = this.container.clientWidth * dpr;
    let h = this.container.clientHeight * dpr;
    const k = Math.min(1, Math.sqrt(MAX_PIXELS / (w * h || 1)));
    w = Math.max(1, Math.round(w * k));
    h = Math.max(1, Math.round(h * k));
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    this._draw();
  }

  // -- DOM --------------------------------------------------------------------

  _build() {
    const root = this.container;
    if (getComputedStyle(root).position === "static") root.style.position = "relative";
    root.classList.add("v360");
    const style = document.createElement("style");
    style.textContent = CSS;
    const c = this.canvas;
    c.className = "v360-canvas";
    c.tabIndex = 0;
    c.setAttribute("aria-label", "360°-Ansicht: Ziehen oder Pfeiltasten zum Umschauen, Mausrad, zwei Finger oder +/− zum Zoomen");
    const sw = (this._switch = document.createElement("div"));
    sw.className = "v360-switch";
    sw.setAttribute("role", "group");
    sw.innerHTML = `
      <button data-mode="360" title="Als 360°-Ansicht zeigen">360°</button><button data-mode="flat" title="Das Bild so zeigen, wie es in der Datei steht">Flach</button>`;
    const bar = (this._bar = document.createElement("div"));
    bar.className = "v360-bar";
    const fs = document.fullscreenEnabled && root.requestFullscreen;
    bar.innerHTML = `
      <button class="v360-play" title="Abspielen">${icon(ICON_PLAY)}</button>
      <input class="v360-seek" type="range" min="0" max="0" step="any" value="0" aria-label="Position">
      <span class="v360-time">0:00 / –:––</span>
      <button class="v360-mute" title="Ton aus" hidden>${icon(ICON_SOUND)}</button>
      <button class="v360-north" title="Blick zurücksetzen (nach vorn)">${icon(ICON_NORTH)}</button>
      ${fs ? `<button class="v360-fs" title="Vollbild">${icon(ICON_FULL)}</button>` : ""}`;
    const hint = (this._hintEl = document.createElement("div"));
    hint.className = "v360-hint";
    hint.textContent = "Ziehen zum Umschauen";
    this._nodes = [style, c, sw, bar, hint];
    root.append(...this._nodes);
    this._seek = bar.querySelector(".v360-seek");
    this._time = bar.querySelector(".v360-time");
  }

  _wire() {
    const v = this.video;
    const sig = { signal: this._abort.signal };
    const on = (target, type, fn, opts) => target.addEventListener(type, fn, { ...sig, ...opts });

    on(v, "play", () => {
      this._armFrame();
      this._updateBar();
    });
    on(v, "pause", () => this._updateBar());
    // A new frame while paused (seeked, first data): catch it now and when it is presented.
    for (const type of ["seeked", "loadeddata"]) on(v, type, () => this.mode === "360" && this._frame());
    for (const type of ["timeupdate", "durationchange", "loadedmetadata", "volumechange"]) on(v, type, () => this._updateBar());
    // Not playable (HEVC in Firefox): show the video with its native controls and error state.
    on(v, "error", () => this.mode === "360" && this.setMode("flat"));

    for (const b of this._switch.querySelectorAll("button")) on(b, "click", () => this.setMode(b.dataset.mode));
    const bar = this._bar;
    on(bar.querySelector(".v360-play"), "click", () => this._togglePlay());
    on(bar.querySelector(".v360-mute"), "click", () => (v.muted = !v.muted));
    on(bar.querySelector(".v360-north"), "click", () => this.resetView());
    const fs = bar.querySelector(".v360-fs");
    if (fs) {
      on(fs, "click", () => {
        if (this.container.matches(":fullscreen")) document.exitFullscreen().catch(() => {});
        else this.container.requestFullscreen().catch(() => {});
      });
      on(document, "fullscreenchange", () => {
        const full = this.container.matches(":fullscreen");
        fs.title = full ? "Vollbild beenden" : "Vollbild";
        fs.innerHTML = icon(full ? ICON_FULL_EXIT : ICON_FULL);
      });
    }
    on(this._seek, "input", () => {
      this._scrubbing = true;
      this._seekTo(Number(this._seek.value));
    });
    on(this._seek, "change", () => {
      this._scrubbing = false;
      this._updateBar();
    });

    this._wirePointer(on);
    const c = this.canvas;
    on(c, "keydown", (e) => this._onKey(e));
    // The context can be taken away (GPU reset, too many contexts); build it again when it is back.
    on(c, "webglcontextlost", (e) => {
      e.preventDefault();
      this._cancelFrame();
    });
    on(c, "webglcontextrestored", () => {
      this._initGl();
      this._resize();
      this._frame();
    });
    this._ro = new ResizeObserver(() => this.mode === "360" && this._resize());
    this._ro.observe(this.container);
  }

  _wirePointer(on) {
    const c = this.canvas;
    const pts = new Map(); // pointerId -> {x, y}
    let base = null; // state at the start of the current gesture
    let moved = false; // a drag or pinch, not a click
    let vel = { yaw: 0, pitch: 0, t: 0 }; // degrees per ms, for the inertia

    const centre = () => {
      let x = 0;
      let y = 0;
      for (const p of pts.values()) {
        x += p.x;
        y += p.y;
      }
      return { x: x / pts.size, y: y / pts.size };
    };
    const spread = () => {
      const [a, b] = [...pts.values()];
      return b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
    };
    // Start over from the current view whenever a finger is added or lifted.
    const rebase = () => {
      base = pts.size ? { ...centre(), dist: spread(), yaw: this.yaw, pitch: this.pitch, fov: this.fov } : null;
    };

    on(c, "pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      c.setPointerCapture(e.pointerId);
      c.focus({ preventScroll: true });
      if (!pts.size) {
        moved = false;
        vel = { yaw: 0, pitch: 0, t: e.timeStamp };
      }
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pts.size > 1) moved = true;
      cancelAnimationFrame(this._inertiaRaf);
      rebase();
    });
    const move = (e) => {
      if (!pts.has(e.pointerId) || !base) return;
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      const p = centre();
      const dx = p.x - base.x;
      const dy = p.y - base.y;
      if (!moved && Math.hypot(dx, dy) < 5) return;
      if (!moved) {
        moved = true;
        c.classList.add("drag");
        this._dragged();
      }
      if (pts.size > 1 && base.dist) this.fov = clamp((base.fov * base.dist) / (spread() || 1), MIN_FOV, MAX_FOV);
      // The picture follows the pointer: degrees per CSS pixel across the longer side.
      const k = this.fov / Math.max(c.clientWidth, c.clientHeight, 1);
      const yaw = base.yaw - dx * k;
      const pitch = clamp(base.pitch + dy * k, -90, 90);
      const dt = Math.max(1, e.timeStamp - vel.t);
      const s = Math.min(1, dt / 50); // smoothed over the last ~50 ms
      vel = {
        yaw: vel.yaw * (1 - s) + (wrap(yaw - this.yaw) / dt) * s,
        pitch: vel.pitch * (1 - s) + ((pitch - this.pitch) / dt) * s,
        t: e.timeStamp,
      };
      this._look(yaw, pitch);
    };
    on(c, "pointermove", move);
    const up = (e) => {
      if (e.type === "pointerup") move(e); // the last stretch may not have come as a move
      if (!pts.delete(e.pointerId)) return;
      rebase();
      if (pts.size) return;
      c.classList.remove("drag");
      // A click (no drag) plays / pauses, like on the video itself.
      if (!moved) {
        if (e.type === "pointerup") this._togglePlay();
      } else if (e.timeStamp - vel.t < 60) {
        this._glide(vel.yaw, vel.pitch);
      }
    };
    on(c, "pointerup", up);
    on(c, "pointercancel", up);
    on(c, "wheel", (e) => {
      e.preventDefault();
      // Pixels, lines or pages; one notch of a mouse wheel is about 100 px.
      const px = e.deltaY * (e.deltaMode === 1 ? 33 : e.deltaMode === 2 ? 400 : 1);
      this._zoom(this.fov * Math.exp(px * 0.0015));
    }, { passive: false });
    // A double click must not pass on to the page (HA's more-info, text selection).
    on(c, "dblclick", (e) => e.preventDefault());
  }

  /** Let a flicked view drift on and slow down. */
  _glide(vYaw, vPitch) {
    if (Math.abs(vYaw) + Math.abs(vPitch) < 0.02) return;
    let last = performance.now();
    const step = (now) => {
      const dt = Math.min(50, now - last);
      last = now;
      const k = Math.exp(-dt / 250);
      vYaw *= k;
      vPitch *= k;
      this._look(this.yaw + vYaw * dt, clamp(this.pitch + vPitch * dt, -90, 90));
      this._inertiaRaf = Math.abs(vYaw) + Math.abs(vPitch) > 0.003 ? requestAnimationFrame(step) : null;
    };
    this._inertiaRaf = requestAnimationFrame(step);
  }

  _onKey(e) {
    if (e.altKey || e.ctrlKey || e.metaKey) return;
    const step = this.fov / 12;
    switch (e.key) {
      case "ArrowLeft":
        this._look(this.yaw - step, this.pitch);
        break;
      case "ArrowRight":
        this._look(this.yaw + step, this.pitch);
        break;
      case "ArrowUp":
        this._look(this.yaw, clamp(this.pitch + step, -90, 90));
        break;
      case "ArrowDown":
        this._look(this.yaw, clamp(this.pitch - step, -90, 90));
        break;
      case "+":
      case "=":
        this._zoom(this.fov / 1.15);
        break;
      case "-":
      case "_":
        this._zoom(this.fov * 1.15);
        break;
      case " ":
      case "k":
        this._togglePlay();
        break;
      default:
        return;
    }
    e.preventDefault();
    e.stopPropagation();
  }

  _look(yaw, pitch) {
    this.yaw = wrap(yaw);
    this.pitch = pitch;
    // The needle points where "forward" lies from the current view.
    const needle = this._bar.querySelector(".v360-north svg");
    if (needle) needle.style.transform = `rotate(${-this.yaw}deg)`;
    this._requestDraw();
  }

  _zoom(fov) {
    this.fov = clamp(fov, MIN_FOV, MAX_FOV);
    this._requestDraw();
  }

  _togglePlay() {
    const v = this.video;
    if (v.paused || v.ended) v.play()?.catch(() => {});
    else v.pause();
  }

  /** Jump to second t; before the metadata are in, load them first (preload="none"). */
  _seekTo(t) {
    const v = this.video;
    if (v.readyState >= 1) {
      v.currentTime = t;
    } else {
      const loading = this._pendingSeek != null;
      this._pendingSeek = t;
      if (loading) return; // the last position wins
      v.addEventListener(
        "loadedmetadata",
        () => {
          v.currentTime = this._pendingSeek;
          this._pendingSeek = null;
        },
        { once: true, signal: this._abort.signal },
      );
      v.preload = "metadata";
      v.load();
    }
    this._updateBar();
  }

  _updateBar() {
    if (this._dead) return;
    const v = this.video;
    const hint = typeof this._duration === "function" ? this._duration() : this._duration;
    const dur = Number.isFinite(v.duration) && v.duration > 0 ? v.duration : hint || 0;
    const now = this._pendingSeek ?? v.currentTime;
    const play = this._bar.querySelector(".v360-play");
    const playing = !v.paused && !v.ended;
    if (play.dataset.playing !== String(playing)) {
      play.dataset.playing = String(playing);
      play.title = playing ? "Anhalten" : "Abspielen";
      play.innerHTML = icon(playing ? ICON_PAUSE : ICON_PLAY);
    }
    this._seek.max = String(dur);
    this._seek.disabled = !dur;
    if (!this._scrubbing) this._seek.value = String(Math.min(now, dur));
    this._time.textContent = `${fmtClock(now)} / ${dur ? fmtClock(dur) : "–:––"}`;
    // Mute only for videos with sound (the camera's proxy has none).
    const mute = this._bar.querySelector(".v360-mute");
    if (mute.hidden && (v.mozHasAudio || v.webkitAudioDecodedByteCount > 0 || v.audioTracks?.length)) mute.hidden = false;
    if (mute.dataset.muted !== String(v.muted)) {
      mute.dataset.muted = String(v.muted);
      mute.title = v.muted ? "Ton an" : "Ton aus";
      mute.innerHTML = icon(v.muted ? ICON_MUTED : ICON_SOUND);
    }
  }

  // -- hint ---------------------------------------------------------------------

  /** "Ziehen zum Umschauen" for a moment, until someone has dragged once in this browser. */
  _hint(show) {
    clearTimeout(this._hintTimer);
    if (show && readFlag(HINT_KEY)) show = false;
    this._hintEl.classList.toggle("show", show);
    if (show) this._hintTimer = setTimeout(() => this._hint(false), 3000);
  }

  _dragged() {
    this._hint(false);
    try {
      localStorage.setItem(HINT_KEY, "1");
    } catch {
      // No storage (private window): the hint shows again next time.
    }
  }
}

function icon(path) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`;
}

function fmtClock(s) {
  s = Math.max(0, Math.floor(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function clamp(x, lo, hi) {
  return Math.min(hi, Math.max(lo, x));
}

/** Angle into -180..180 degrees. */
function wrap(deg) {
  return deg - 360 * Math.round(deg / 360);
}

function readFlag(key) {
  try {
    return !!localStorage.getItem(key);
  } catch {
    return false;
  }
}

// 3×3 rotations, row-major; x right, y up, z forward.
/** About the vertical: + turns forward to the right. */
function rotY(a) {
  const c = Math.cos(a);
  const s = Math.sin(a);
  return [c, 0, s, 0, 1, 0, -s, 0, c];
}

/** About the side axis: + tilts forward up. */
function rotX(a) {
  const c = Math.cos(a);
  const s = Math.sin(a);
  return [1, 0, 0, 0, c, s, 0, -s, c];
}

/** About the forward axis: + turns up to the left. */
function rotZ(a) {
  const c = Math.cos(a);
  const s = Math.sin(a);
  return [c, -s, 0, s, c, 0, 0, 0, 1];
}

function mul(a, b) {
  const out = new Array(9);
  for (let r = 0; r < 3; r++) {
    for (let k = 0; k < 3; k++) out[r * 3 + k] = a[r * 3] * b[k] + a[r * 3 + 1] * b[3 + k] + a[r * 3 + 2] * b[6 + k];
  }
  return out;
}

/** WebGL 1 takes matrices column by column (and cannot transpose on upload). */
function colMajor(m) {
  return new Float32Array([m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8]]);
}
