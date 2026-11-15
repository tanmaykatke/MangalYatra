// Mangal Yatra HUD: draws the map and status from the backend's WebSocket stream.
(() => {
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  // ?device=1 on the Pi Touch Display 2 (also auto-detected on touch screens): hides laptop helpers
  const DEVICE = params.has("device") || params.has("helmet") || matchMedia("(pointer: coarse)").matches;
  if (DEVICE) document.body.classList.add("device");
  const LAYER_GROUPS = {
    imagery: ["imagery"],
    slope: ["slope"],
    hazards: ["hz-fill", "hz-line", "hz-label"],
    route: ["route-line"],
    science: ["wp-circle", "wp-label"],
  };
  const LAYER_ORDER = Object.keys(LAYER_GROUPS);
  let ws, config = null, state = null, map, mapReady = false, zoom = null;
  let visible = { imagery: true, slope: false, hazards: true, route: true, science: true };
  let cycleIdx = 0;

  // ---------- mirroring for the beam-splitter visor ----------
  const mirrorModes = ["", "mirror-y", "mirror-x"];
  let mirrorIdx = Math.max(0, mirrorModes.indexOf(params.get("mirror") ? "mirror-" + params.get("mirror") : ""));
  const applyMirror = () => { document.body.classList.remove("mirror-x", "mirror-y"); if (mirrorModes[mirrorIdx]) document.body.classList.add(mirrorModes[mirrorIdx]); };
  applyMirror();

  // ---------- map ----------
  function initMap() {
    const m = config.mission;
    zoom = m.zoom || 15;
    map = new maplibregl.Map({
      container: "map",
      style: { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#000" } }] },
      center: m.center, zoom, pitch: 45, bearing: 0, interactive: false, attributionControl: false,
    });
    map.on("load", () => {
      const base = "/mission/";
      const maxzoom = m.tile_maxzoom || 18;
      if (config.tiles.imagery) {
        map.addSource("imagery", { type: "raster", tiles: [base + "tiles/imagery/{z}/{x}/{y}.png"], tileSize: 256, maxzoom });
        map.addLayer({ id: "imagery", type: "raster", source: "imagery", paint: { "raster-opacity": 0.85 } });
      }
      if (config.tiles.slope) {
        map.addSource("slope", { type: "raster", tiles: [base + "tiles/slope/{z}/{x}/{y}.png"], tileSize: 256, maxzoom });
        map.addLayer({ id: "slope", type: "raster", source: "slope", paint: { "raster-opacity": 0.5 } });
      }
      map.addSource("hazards", { type: "geojson", data: base + "hazards.geojson" });
      map.addLayer({ id: "hz-fill", type: "fill", source: "hazards", paint: { "fill-color": ["match", ["get", "kind"], "soft_ground", "#ffc93c", "#ff4d4d"], "fill-opacity": 0.28 } });
      map.addLayer({ id: "hz-line", type: "line", source: "hazards", paint: { "line-color": ["match", ["get", "kind"], "soft_ground", "#ffc93c", "#ff4d4d"], "line-width": 2 } });
      map.addSource("route", { type: "geojson", data: base + "route.geojson" });
      map.addLayer({ id: "route-line", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#39f3ff", "line-width": 5, "line-dasharray": [2, 1] } });
      map.addSource("wps", { type: "geojson", data: base + "waypoints.geojson" });
      map.addLayer({ id: "wp-circle", type: "circle", source: "wps", paint: {
        "circle-radius": 9, "circle-stroke-width": 3, "circle-color": "#000",
        "circle-stroke-color": ["match", ["get", "kind"], "science", "#5dff8f", "base", "#ffffff", "#39f3ff"] } });
      // Labels are HTML markers so no font server is needed (works fully offline).
      addHtmlLabels(base);
      map.setPadding({ top: window.innerHeight * 0.24, bottom: 80 });
      mapReady = true;
      applyLayers();
    });
  }

  const htmlLabels = { hazards: [], science: [] };
  async function addHtmlLabels(base) {
    const mk = (coord, text, color, group) => {
      const el = document.createElement("div");
      el.textContent = text;
      el.style.cssText = `color:${color};font:bold 13px var(--font);text-shadow:0 0 3px #000,0 0 3px #000;white-space:nowrap;transform:translateY(-18px)`;
      htmlLabels[group].push(new maplibregl.Marker({ element: el, anchor: "bottom", pitchAlignment: "viewport", rotationAlignment: "viewport" }).setLngLat(coord).addTo(map));
    };
    const [hz, wp] = await Promise.all([fetch(base + "hazards.geojson").then((r) => r.json()), fetch(base + "waypoints.geojson").then((r) => r.json())]);
    // Real pipeline output has many small polygons of the same kind: label each kind only once per ~200 m.
    const placed = [];
    const near = (a, b) => Math.hypot((a[0] - b[0]) * Math.cos(a[1] * Math.PI / 180), a[1] - b[1]) * 59274 < 200;
    hz.features.forEach((f) => {
      const ring = f.geometry.coordinates[0];
      const c = ring.slice(0, -1).reduce((a, p) => [a[0] + p[0] / (ring.length - 1), a[1] + p[1] / (ring.length - 1)], [0, 0]);
      if (placed.some((p) => p.name === f.properties.name && near(p.c, c))) return;
      placed.push({ name: f.properties.name, c });
      mk(c, "⚠ " + f.properties.name, f.properties.kind === "soft_ground" ? "#ffc93c" : "#ff4d4d", "hazards");
    });
    wp.features.forEach((f) => mk(f.geometry.coordinates, f.properties.id + " " + f.properties.name, f.properties.kind === "science" ? "#5dff8f" : "#ffffff", "science"));
    applyLayers();
  }

  function applyLayers() {
    if (!mapReady) return;
    for (const [name, ids] of Object.entries(LAYER_GROUPS)) {
      ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", visible[name] ? "visible" : "none"));
      (htmlLabels[name === "hazards" ? "hazards" : name === "science" ? "science" : "_"] || []).forEach((mk) => (mk.getElement().style.display = visible[name] ? "" : "none"));
    }
    $("layers").innerHTML = LAYER_ORDER.map((n, i) => {
      const na = (n === "imagery" || n === "slope") && !(config && config.tiles[n]);
      return `<span class="chip ${visible[n] ? "on" : ""} ${na ? "na" : ""}" data-l="${n}" title="${na ? "Run the data pipeline to create these tiles" : ""}">${i + 1} ${n}</span>`;
    }).join("");
  }
  $("layers").addEventListener("click", (e) => { const l = e.target.dataset.l; if (l) toggleLayer(l); });
  function toggleLayer(l) { visible[l] = !visible[l]; applyLayers(); }

  // ---------- compass tape ----------
  function bearing(a, b) {
    const r = Math.PI / 180, [l1, p1, l2, p2] = [a[0] * r, a[1] * r, b[0] * r, b[1] * r];
    const y = Math.sin(l2 - l1) * Math.cos(p2), x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(l2 - l1);
    return (Math.atan2(y, x) / r + 360) % 360;
  }
  function drawTape(h, wpBearing) {
    const W = $("compass").clientWidth, span = 60, ppd = W / (2 * span);
    const names = { 0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SW", 270: "W", 315: "NW" };
    let html = "";
    for (let a = Math.ceil((h - span) / 15) * 15; a <= h + span; a += 15) {
      const n = ((a % 360) + 360) % 360, x = W / 2 + (a - h) * ppd;
      html += `<span class="${names[n] !== undefined ? "card" : ""}" style="left:${x}px">${names[n] ?? n}</span>`;
    }
    if (wpBearing != null) {
      const d = ((wpBearing - h + 540) % 360) - 180;
      const x = Math.abs(d) <= span ? W / 2 + d * ppd : d < 0 ? 10 : W - 10;
      html += `<span class="wp" style="left:${x}px">${Math.abs(d) <= span ? "◆" : d < 0 ? "◀" : "▶"}</span>`;
    }
    $("tape").innerHTML = html;
  }

  // ---------- render state ----------
  function setVal(id, v, warn, crit) {
    const el = $(id); el.textContent = v ?? "--";
    el.className = crit ? "crit" : warn ? "warn" : "";
  }
  function render(s) {
    state = s;
    $("hdg").textContent = String(Math.round(s.heading)).padStart(3, "0");
    $("hdgl").textContent = s.heading_label;
    const wp = s.next_wp;
    $("wpName").textContent = wp ? `${wp.id} ${wp.name}` : "Route complete";
    $("wpDist").textContent = wp ? wp.dist_m : "--";
    $("wpTask").textContent = wp ? wp.task : "Return to habitat.";
    $("remain").textContent = s.remaining_m;
    $("mt").textContent = s.mission_time_min;
    $("paused").textContent = s.paused ? "· PAUSED" : "";
    $("btnPause").innerHTML = s.paused ? '<span class="ico">&#9654;</span>Resume' : '<span class="ico">&#9208;</span>Pause';
    document.querySelector(".tb.sos").classList.toggle("on", s.alerts.some((a) => a.id === "sos"));
    drawTape(s.heading, wp ? bearing(s.pos, wp.coord) : null);

    const u = s.suit, c = s.conditions;
    setVal("o2", u.o2_min, u.o2_min < 120, u.o2_min < 60);
    setVal("bat", u.battery_pct, u.battery_pct < 30, u.battery_pct < 15);
    setVal("stemp", u.temp_c);
    setVal("spres", u.pressure_kpa);
    setVal("dust", c.dust_opacity_tau, c.dust_opacity_tau >= (c.dust_warn_tau || 1));
    setVal("air", c.air_temp_c);
    setVal("rad", c.radiation_usv_per_day, false, c.solar_particle_event);

    $("alerts").innerHTML = s.alerts.map((a) => `<div class="alert ${a.level}">${a.level === "critical" ? "▲" : "⚠"} ${a.msg}</div>`).join("");

    if (mapReady) map.easeTo({ center: s.pos, bearing: s.heading, zoom, duration: 220, easing: (t) => t });
  }

  // ---------- speech out (browser fallback when Piper isn't installed) ----------
  function speak(text) {
    $("voiceText").textContent = "▶ " + text;
    if (params.has("mute")) return;                 // ?mute=1: silent copy (second screen, previews)
    if ("speechSynthesis" in window) {
      speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text); u.rate = 1.05;
      speechSynthesis.speak(u);
    }
  }

  // ---------- Earth link (Twilio relay through the habitat) ----------
  let earth = null;
  const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.round(s) % 60).padStart(2, "0")}`;
  function setEarth(s) {
    const now = Date.now();
    earth = { ...s, outArr: s.outbox.map((m) => now + m.eta_s * 1000), inArr: s.inbox_eta_s.map((e) => now + e * 1000) };
    const d = s.delay_s;
    $("edelay").textContent = d < 90 ? d : (d / 60).toFixed(1);
    $("edelayU").textContent = d < 90 ? "s" : "min";
    drawEarthLine();
  }
  function drawEarthLine() {
    if (!earth) return;
    const now = Date.now(), parts = [earth.live ? "SMS live" : "simulated"];
    if (earth.delay_s !== earth.real_delay_s) parts.push(`real ${(earth.real_delay_s / 60).toFixed(0)} min`);
    earth.outArr.forEach((t) => parts.push(`↑ to Earth ${mmss(Math.max(0, (t - now) / 1000))}`));
    earth.inArr.forEach((t) => parts.push(`↓ from Earth ${mmss(Math.max(0, (t - now) / 1000))}`));
    $("earthLine").textContent = "📡 " + parts.join(" · ");
  }
  setInterval(drawEarthLine, 1000);

  // ---------- mission report (written by Gemini at Mission Control on Earth) ----------
  let reportStage = null;
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function mdToHtml(md) {                       // tiny, safe Markdown: escape first, then headings, bullets, bold
    return esc(md).split("\n").map((l) => {
      if (/^#{1,3}\s/.test(l)) return `<div class="rh">${l.replace(/^#{1,3}\s/, "")}</div>`;
      if (/^\s*[-*]\s/.test(l)) return `<div class="rb">• ${l.replace(/^\s*[-*]\s/, "")}</div>`;
      return l.trim() ? `<div>${l}</div>` : "";
    }).join("").replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  }
  function setReportStatus(m) {
    reportStage = (m.stage === "failed" || m.stage === "cancelled") ? null : { ...m, until: Date.now() + (m.eta_s || 0) * 1000 };
    drawReportStatus();
  }
  function drawReportStatus() {
    if (!reportStage) return;
    const left = mmss(Math.max(0, (reportStage.until - Date.now()) / 1000));
    const txt = { to_earth: `📄 EVA log → Earth ${left}`, writing: "📄 Gemini is writing the report…",
                  to_mars: `📄 Report → Mars ${left}` }[reportStage.stage];
    if (txt) $("voiceText").textContent = txt;
  }
  setInterval(drawReportStatus, 1000);
  function showReport(r) {
    reportStage = null;
    $("reportBy").textContent = r.by || "";
    $("reportText").innerHTML = mdToHtml(r.text || "");
    $("report").classList.remove("hidden");
    $("voiceText").textContent = "📄 Mission report received. Tap to close";
  }
  $("report").addEventListener("click", () => $("report").classList.add("hidden"));
  let earthTimer;
  function showEarthMsg(m) {
    const el = $("earthMsg");
    const ago = m.travelled_s < 90 ? `${m.travelled_s} s` : `${Math.round(m.travelled_s / 60)} min`;
    el.innerHTML = `<div class="label">📡 Message from Earth · sent ${ago} ago</div><div></div>`;
    el.lastChild.textContent = m.text;            // text from SMS: never inject as HTML
    el.classList.remove("hidden");
    clearTimeout(earthTimer);
    earthTimer = setTimeout(() => el.classList.add("hidden"), 30000);
  }
  $("earthMsg").addEventListener("click", () => $("earthMsg").classList.add("hidden"));

  // ---------- websocket ----------
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }
  function cmd(action, extra = {}) { send({ type: "command", action, ...extra }); }
  function connect() {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => $("conn").classList.add("hidden");
    ws.onclose = () => { $("conn").classList.remove("hidden"); setTimeout(connect, 1500); };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      switch (msg.type) {
        case "reload": location.reload(); return;      // dev mode: a HUD file changed
        case "config":
          // Server restarted (e.g. backend code changed): reload to pick up any new HUD code too.
          if (config && config.server_id && msg.server_id !== config.server_id) { location.reload(); return; }
          const first = !config; config = msg;
          if (msg.earth) setEarth(msg.earth);
          if (!config.tiles.imagery) visible.imagery = false;
          if (first) initMap(); else applyLayers();
          break;
        case "state": render(msg); break;
        case "say": if (msg.spoken) $("voiceText").textContent = "▶ " + msg.text; else speak(msg.text); break;
        case "heard": $("voiceText").textContent = "“" + msg.text + "”"; break;
        case "listening": $("mic").classList.toggle("on", msg.on); if (msg.on) $("voiceText").textContent = "Listening..."; break;
        case "earth": setEarth(msg); break;
        case "earth_msg": showEarthMsg(msg); break;
        case "report_status": setReportStatus(msg); break;
        case "report": showReport(msg); break;
        case "command":
          if (msg.action === "toggle_layer") toggleLayer(msg.layer);
          else if (msg.action === "zoom_in") zoom = Math.min(19, zoom + 0.75);
          else if (msg.action === "zoom_out") zoom = Math.max(12, zoom - 0.75);
          else if (msg.action === "mirror") { mirrorIdx = (mirrorIdx + 1) % mirrorModes.length; applyMirror(); }
          else if (msg.action === "cycle_layer") { const l = LAYER_ORDER[cycleIdx++ % LAYER_ORDER.length]; toggleLayer(l); speak(`${l} ${visible[l] ? "on" : "off"}`); }
          break;
      }
    };
  }

  // ---------- keyboard (stands in for buttons, IMU and mic on the laptop) ----------
  const form = $("cmdForm"), input = $("cmdInput");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (input.value.trim()) send({ type: "voice_text", text: input.value.trim() });
    input.value = ""; form.classList.add("hidden"); input.blur();
  });
  document.addEventListener("keydown", (e) => {
    if (document.activeElement === input) { if (e.key === "Escape") { form.classList.add("hidden"); input.blur(); } return; }
    const k = e.key;
    if (k === "Enter") { form.classList.remove("hidden"); input.focus(); e.preventDefault(); }
    else if (k === "ArrowLeft") cmd("turn", { delta: -15 });
    else if (k === "ArrowRight") cmd("turn", { delta: 15 });
    else if (k === "n" || k === "N") cmd("next_waypoint");
    else if (k === "p" || k === "P") cmd(state && state.paused ? "resume" : "pause");
    else if (k === "+" || k === "=") { zoom = Math.min(19, zoom + 0.75); }
    else if (k === "-") { zoom = Math.max(12, zoom - 0.75); }
    else if (k === "s" || k === "S") cmd("sos");
    else if (k === "r" || k === "R") cmd("recenter");
    else if (k === "0") cmd("restart");
    else if (k === "e" || k === "E") send({ type: "earth_test" });
    else if (k === "g" || k === "G") cmd("report");
    else if (k === "m" || k === "M") { mirrorIdx = (mirrorIdx + 1) % mirrorModes.length; applyMirror(); }
    else if (k === "h" || k === "H") $("help").classList.toggle("hidden");
    else if (k >= "1" && k <= "5") toggleLayer(LAYER_ORDER[+k - 1]);
  });
  if (!DEVICE) $("help").classList.remove("hidden");

  // ---------- touch toolbar ----------
  document.querySelectorAll("#toolbar [data-act]").forEach((b) => b.addEventListener("click", () => {
    const a = b.dataset.act;
    if (a === "zoom_in") zoom = Math.min(19, zoom + 0.75);
    else if (a === "zoom_out") zoom = Math.max(12, zoom - 0.75);
    else if (a === "pause") cmd(state && state.paused ? "resume" : "pause");
    else cmd(a);
  }));
  // Talk: hold to speak when the Pi has a mic + Vosk (offline). Otherwise tap to speak using the
  // browser's own speech recognition (Edge/Chrome; needs internet). If neither exists, tap to type.
  const talk = $("btnTalk");
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;
  function browserListen() {
    if (rec) { rec.abort(); return; }                       // tap again to cancel
    rec = new SR();
    rec.lang = "en-US"; rec.interimResults = true; rec.maxAlternatives = 1;
    let finalText = "";
    const done = () => { rec = null; talk.classList.remove("held"); $("mic").classList.remove("on"); };
    rec.onstart = () => { talk.classList.add("held"); $("mic").classList.add("on"); $("voiceText").textContent = "Listening... speak now"; };
    rec.onresult = (ev) => {
      const r = ev.results[ev.results.length - 1];
      $("voiceText").textContent = "“" + r[0].transcript + "”";
      if (r.isFinal) finalText = r[0].transcript.trim();
    };
    rec.onerror = (ev) => {
      const why = { "not-allowed": "Microphone blocked: click the mic icon in the address bar and allow it",
                    "no-speech": "Didn't hear anything. Tap Talk and try again",
                    "network": "Speech service unreachable (needs internet). Press Enter to type instead",
                    "audio-capture": "No microphone found" }[ev.error];
      if (why) $("voiceText").textContent = why;
    };
    rec.onend = () => { done(); if (finalText) send({ type: "voice_text", text: finalText }); };
    rec.start();
  }
  const talkDown = (e) => {
    e.preventDefault();
    if (config && config.stt) { talk.classList.add("held"); send({ type: "ptt", down: true }); }
    else if (SR) browserListen();
    else { form.classList.remove("hidden"); input.focus(); }
  };
  const talkUp = () => {
    if (config && config.stt && talk.classList.contains("held")) { talk.classList.remove("held"); send({ type: "ptt", down: false }); }
  };
  talk.addEventListener("pointerdown", talkDown);
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => talk.addEventListener(ev, talkUp));
  window.addEventListener("resize", () => mapReady && map.setPadding({ top: window.innerHeight * 0.24, bottom: 80 }));
  connect();
})();
