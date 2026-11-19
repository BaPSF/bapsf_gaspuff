// The pressure page. Everything shown comes from the server: a frame over the WebSocket twice a second (session.js)
// and api/history for the history chart. The page sends nothing but those history requests.
(function () {
  "use strict";

  const HOUR = 3600, DAY = 86400;
  const RANGES = [["1 h", HOUR], ["8 h", 8 * HOUR], ["24 h", DAY], ["7 d", 7 * DAY], ["30 d", 30 * DAY]];
  const BUCKETS = 480;                    // time buckets of the history chart
  const HISTORY_REFRESH_MS = 60 * 1000;   // the history chart reloads this often while it shows a range up to now
  const SERIES_COLORS = 8;                // --g1 .. --g8 in index.html
  // Gauge status codes of the IOC (<gauge>:STATUS): 0 to 7 from the driver, 8 when the controller did not answer.
  const STATUS_TEXT = ["Measuring", "Underrange", "Overrange", "Sensor error", "Sensor off", "No gauge",
                       "Identification error", "Unknown status", "No reply from the controller"];
  const STATUS_NO_GAUGE = 5, STATUS_NO_REPLY = 8;

  const byId = id => document.getElementById(id);
  const token = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  // Gauge names and models come from the IOC's config and the controller: never trusted as HTML.
  const esc = text => String(text).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const SUPERSCRIPT = {"-": "⁻", "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"};
  const power = e => "10" + String(e).split("").map(c => SUPERSCRIPT[c]).join("");
  function sci(v) {
    if (v == null || !Number.isFinite(v)) return "--";
    if (v === 0) return "0";
    const [m, e] = v.toExponential(2).split("e");
    return `${m.replace("-", "−")} × ${power(Number(e))}`;
  }
  const torr = v => `<span class="nw">${sci(v)} Torr</span>`;
  const positive = v => (v > 0 ? v : null);  // the charts have a log axis
  function multiplier(v) {  // 5.9, 0.18, 1.0
    const text = String(Number(v.toPrecision(3)));
    return /[.e]/.test(text) ? text : text + ".0";
  }

  //=============================================================================================================================================
  // State

  let meta = null;            // what the server says about the gauges and the gas; arrives with the first frame
  let zone = "America/Los_Angeles";  // meta.zone: every time is shown in the host's zone, never the browser's
  let keys = [];              // gauge keys in the column order of a frame's "p"
  let live = {t: [], p: {}};  // the last 5 minutes: p[key] is as long as t
  let latest = null, gas = null, gasChanges = [], waiting = true, ageS = null, hostNow = null;
  let closedNote = null;      // set once the socket has closed
  let colors = {}, livePlot = null, historyPlot = null, livePaused = false;
  let rangeSeconds = DAY, rawView = false, zoom = null;  // zoom: {start, end} after a drag on the history chart
  let history = null;         // the history chart's data: {start, end, raw, t, series, gas}
  let historyError = "", historyRequest = 0;

  const gauges = () => (meta ? meta.gauges : []);
  const zoneLabel = () => zone.split("/").pop().replace(/_/g, " ");
  const clockText = (s, withDate) => new Date(s * 1e3).toLocaleString("en-US", {timeZone: zone, hour12: false,
    ...(withDate ? {month: "short", day: "numeric"} : {}), hour: "2-digit", minute: "2-digit", second: withDate ? undefined : "2-digit"});
  const swatch = i => `var(--g${i % SERIES_COLORS + 1})`;

  //=============================================================================================================================================
  // Header, bars, gas row and tiles

  function renderHeader() {
    const status = byId("status");
    status.textContent = closedNote ? "Disconnected" : !meta ? "Connecting" : waiting ? "Waiting" : "Live";
    status.className = "pill" + (closedNote ? " off" : meta && !waiting ? " live" : "");
    byId("clock").textContent = hostNow == null ? "" : `Host clock ${clockText(hostNow)}, ${zoneLabel()} time`;

    const bar = byId("waiting");
    bar.hidden = !meta || !waiting;
    if (!bar.hidden) {
      const values = latest ? Object.values(latest.gauges) : [];
      bar.textContent = !latest ? "Waiting for the first reading."
        : `Waiting for readings: the last one came ${Math.round(ageS)} s ago, at ${clockText(latest.time)}.`
          + (values.length && values.every(v => v.status === STATUS_NO_REPLY) ? " The gauge controller did not answer." : "");
    }
  }

  function renderAlarmBar() {
    const raised = gauges().filter(g => g.base_level);
    const bar = byId("alarm-bar");
    bar.hidden = raised.length === 0;
    bar.className = "bar " + (raised.some(g => g.base_level === "alarm") ? "alarm" : "warn");
    bar.innerHTML = raised.map(g => {
      const base = latest && latest.gauges[g.key] ? latest.gauges[g.key].base : null;
      const value = base == null ? "" : `: ${torr(base)}`;
      return g.base_level === "alarm"
        ? `<div>■ Base pressure alarm on ${esc(g.key)}${value}${g.alarm == null ? "" : `, alarm limit ${torr(g.alarm)}`}.</div>`
        : `<div>▲ Base pressure high on ${esc(g.key)}${value}${g.warn == null ? "" : `, warning limit ${torr(g.warn)}`}.</div>`;
    }).join("");
  }

  function renderGas() {
    const species = meta && meta.species.length ? meta.species : gas ? [gas] : [];
    byId("gases").innerHTML = species.map(name =>
      `<span class="choice${name === gas ? " on" : ""}"${name === gas ? ' aria-current="true"' : ""}>${esc(name)}</span>`).join("");
    const since = meta && meta.gas_since != null ? ` since ${clockText(meta.gas_since, true)}` : "";
    byId("gas-since").textContent = gas ? `${gas}${since}. Each gauge has its own multiplier, shown on its tile.` : "";
  }

  function renderTiles() {
    const baseLabel = meta && meta.base_window_h != null ? `Base, ${Number(meta.base_window_h.toPrecision(3))} h` : "Base";
    byId("tiles").innerHTML = gauges().map((g, i) => {
      const v = latest ? latest.gauges[g.key] : null;
      const name = `<div class="name"><span class="swatch" style="background: ${swatch(i)}"></span>${esc(g.key)}</div>
        <div class="line">${esc(g.name)}</div>`;
      if (v && v.status === STATUS_NO_GAUGE) {
        return `<div class="tile empty dim">${name}<div class="value">--</div><div class="line">${STATUS_TEXT[STATUS_NO_GAUGE]}</div></div>`;
      }
      const level = g.base_level;
      const status = v ? STATUS_TEXT[v.status] || `Status ${v.status}` : "No reading yet";
      const factor = v && v.gas_factor != null ? `<span class="nw">× ${multiplier(v.gas_factor)}</span>` : "";
      const reads = v ? `<br>Gauge reads <span class="nw">${sci(v.p_raw)}</span> ${factor}` : "";
      const flag = level === "alarm" ? `<div class="flag">■ Base pressure alarm</div>`
        : level === "warn" ? `<div class="flag">▲ Base pressure high</div>` : "";
      const base = v && v.base != null ? `<span class="nw">${sci(v.base)}</span>` : "not ready yet";
      const first = g.warn != null ? g.warn : g.alarm;
      const limit = first == null ? "no limit set"
        : level === "alarm" && g.alarm != null ? `alarm limit <span class="nw">${sci(g.alarm)}</span>`
        : level === "warn" && g.warn != null ? `warning limit <span class="nw">${sci(g.warn)}</span>`
        : v && v.base != null && !level ? `✓ below <span class="nw">${sci(first)}</span>`
        : `limit <span class="nw">${sci(first)}</span>`;
      // The controller's own gas setting must stay on nitrogen, or the reading is corrected twice (the IOC raises this).
      const twice = g.device_gas_alarm ? `<div class="flag">▲ Controller gas setting is ${esc(g.device_gas)}: corrected twice</div>` : "";
      return `<div class="tile ${level} ${waiting || !v || v.p == null ? "dim" : ""}">${name}
        <div class="value">${v ? sci(v.p) : "--"} <span>Torr</span></div>
        <div class="line">${esc(status)}${g.model ? ` · ${esc(g.model)}` : ""}${reads}</div>
        ${flag}${twice}
        <div class="line">${baseLabel}: ${base}<br>${limit}</div>
      </div>`;
    }).join("");
  }

  function renderNotes() {
    const period = meta ? meta.period_s : null;
    const rate = period == null ? "" : period <= 1 ? `, ${Math.round(1 / period)} a second` : `, one every ${Number(period.toPrecision(2))} s`;
    byId("live-note").textContent = `every reading${rate}, corrected for gas`;
    byId("history-title").textContent = `mean of each time bucket; host clock, ${zoneLabel()} time`;
  }

  //=============================================================================================================================================
  // Charts

  const plotHeight = () => (window.innerWidth < 600 ? 260 : 340);
  const decades = (u, splits) => splits.map(v => {
    const e = Math.log10(v);
    return Math.abs(e - Math.round(e)) < 1e-6 ? power(Math.round(e)) : "";
  });
  const tzDate = ts => uPlot.tzDate(new Date(ts * 1e3), zone);

  function axes() {
    const common = {stroke: colors.muted, grid: {stroke: colors.grid, width: 1}, ticks: {stroke: colors.grid, width: 1}};
    return [common, {...common, label: "Pressure (Torr)", size: 64, values: decades}];
  }

  const gaugeSeries = () => gauges().map((g, i) => ({label: g.key, stroke: colors.series[i % SERIES_COLORS], width: 2,
                                                      points: {show: false}, spanGaps: false, value: (u, v) => sci(v)}));

  // Whole decades around the data and `also` (the limit lines); a chart with nothing to show still gets an axis.
  const logRange = also => (u, min, max) => {
    for (const v of also()) {
      min = min == null ? v : Math.min(min, v);
      max = max == null ? v : Math.max(max, v);
    }
    // A hair of room, so a reading of exactly 10^-3 is not drawn on the frame.
    return min > 0 && max > 0 ? uPlot.rangeLog(min * 0.999, max * 1.001, 10, true) : [1e-9, 1e-3];
  };
  // The live chart always spans the last 5 minutes up to the newest reading (the host clock before the first), so a
  // pause in the readings shows as a gap and a chart with one reading or none still has a time axis.
  function showLive() {
    livePlot.setData(liveData());
    const end = live.t.length ? live.t[live.t.length - 1] : hostNow == null ? Date.now() / 1e3 : hostNow;
    livePlot.setScale("x", {min: end - (meta ? meta.live_seconds : 300), max: end});
  }

  function drawGasChanges(changes) {
    return u => {
      const ratio = window.devicePixelRatio || 1, {ctx, bbox} = u, {min, max} = u.scales.x;
      ctx.save();
      for (const [when, species] of changes()) {
        if (!(when > min && when < max)) continue;
        const x = Math.round(u.valToPos(when, "x", true));
        ctx.strokeStyle = colors.muted;
        ctx.lineWidth = ratio;
        ctx.setLineDash([2 * ratio, 3 * ratio]);
        ctx.beginPath();
        ctx.moveTo(x, bbox.top);
        ctx.lineTo(x, bbox.top + bbox.height);
        ctx.stroke();
        ctx.setLineDash([]);
        const room = x < bbox.left + bbox.width - 90 * ratio;
        ctx.fillStyle = colors.ink;
        ctx.font = `${12 * ratio}px system-ui, sans-serif`;
        ctx.textAlign = room ? "left" : "right";
        ctx.textBaseline = "top";
        ctx.fillText(species, x + (room ? 5 : -5) * ratio, bbox.top + 4 * ratio);
      }
      ctx.restore();
    };
  }

  // The base-pressure limits apply to the gauge reading, so they are drawn in that view only.
  const limitLines = () => (rawView ? gauges().flatMap(g => [[g.warn, colors.warn, `${g.key} warning limit`, false],
                                                              [g.alarm, colors.alarm, `${g.key} alarm limit`, true]])
                                                .filter(line => line[0] != null) : []);

  function drawLimits(u) {
    const ratio = window.devicePixelRatio || 1, {ctx, bbox} = u;
    ctx.save();
    ctx.beginPath();
    ctx.rect(bbox.left, bbox.top, bbox.width, bbox.height);
    ctx.clip();
    for (const [value, color, text, above] of limitLines()) {
      const y = Math.round(u.valToPos(value, "y", true));
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5 * ratio;
      ctx.setLineDash([6 * ratio, 4 * ratio]);
      ctx.beginPath();
      ctx.moveTo(bbox.left, y);
      ctx.lineTo(bbox.left + bbox.width, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = colors.ink;
      ctx.font = `${12 * ratio}px system-ui, sans-serif`;
      ctx.textAlign = "right";
      ctx.textBaseline = above ? "bottom" : "top";
      ctx.fillText(text, bbox.left + bbox.width - 6 * ratio, y + (above ? -3 : 4) * ratio);
    }
    ctx.restore();
  }

  const liveData = () => [live.t, ...gauges().map(g => live.p[g.key] || live.t.map(() => null))];
  const historyData = () => (history ? [history.t, ...gauges().map(g => history.series[g.key] || history.t.map(() => null))]
                                     : [[], ...gauges().map(() => [])]);

  function setLivePaused(paused) {
    livePaused = paused;
    byId("live-pause").textContent = paused ? "Resume" : "Pause";
    byId("live-pause").classList.toggle("on", paused);
    if (!paused && livePlot) showLive();
  }

  function buildPlots() {
    colors = Object.fromEntries(["ink", "muted", "grid", "warn", "alarm"].map(n => [n, token(`--${n}`)]));
    colors.series = Array.from({length: SERIES_COLORS}, (_, i) => token(`--g${i + 1}`));
    if (livePlot) livePlot.destroy();
    if (historyPlot) historyPlot.destroy();
    const base = {height: plotHeight(), tzDate, axes: axes()};
    livePlot = new uPlot({
      ...base, width: byId("live-plot").clientWidth, series: [{label: "Time"}, ...gaugeSeries()],
      scales: {y: {distr: 3, range: logRange(() => [])}},
      // A drag zooms, and the chart must then stop following the readings or the next frame undoes the zoom.
      hooks: {draw: [drawGasChanges(() => gasChanges)], setSelect: [u => { if (u.select.width > 0) setLivePaused(true); }]},
    }, liveData(), byId("live-plot"));
    livePlot.over.addEventListener("dblclick", () => setLivePaused(false));

    historyPlot = new uPlot({
      ...base, width: byId("history-plot").clientWidth, series: [{label: "Time"}, ...gaugeSeries()],
      // The x axis is the range asked for, not the range that holds data: a night without the store shows as a gap.
      scales: {x: {range: (u, min, max) => (history ? [history.start, history.end] : [min, max])},
               y: {distr: 3, range: logRange(() => limitLines().map(line => line[0]))}},
      // A drag asks the server for that range again, at full detail, in place of stretching the buckets on screen.
      cursor: {drag: {x: true, y: false, setScale: false}},
      hooks: {draw: [drawLimits, drawGasChanges(() => (history ? history.gas.filter(change => change[0] > history.start) : []))],
              setSelect: [u => {
                if (u.select.width <= 0) return;
                const start = u.posToVal(u.select.left, "x"), end = u.posToVal(u.select.left + u.select.width, "x");
                u.setSelect({left: 0, top: 0, width: 0, height: 0}, false);
                if (end - start >= 60) {
                  zoom = {start, end};
                  loadHistory();
                }
              }]},
    }, historyData(), byId("history-plot"));
    historyPlot.over.addEventListener("dblclick", () => {
      zoom = null;
      loadHistory();
    });
  }

  //=============================================================================================================================================
  // History

  // The server sends the start of each bucket that holds data. Buckets without data are left out, so a null is put
  // into every hole or the line would run straight across it.
  function historySeries(body, start, end) {
    const width = (end - start) / BUCKETS, t = [], series = {};
    const names = Object.keys(body.gauges);
    for (const name of names) series[name] = [];
    body.t.forEach((bucket, i) => {
      if (i > 0 && bucket - body.t[i - 1] > 1.5 * width) {
        t.push(body.t[i - 1] + width);
        for (const name of names) series[name].push(null);
      }
      t.push(bucket + width / 2);
      for (const name of names) series[name].push(positive(body.gauges[name].mean[i]));
    });
    return {t, series};
  }

  async function loadHistory() {
    if (!meta || !meta.history || hostNow == null) return;
    const request = ++historyRequest;
    const end = zoom ? zoom.end : hostNow, start = zoom ? zoom.start : end - rangeSeconds, raw = rawView;
    renderHistoryControls();
    try {
      const response = await fetch(`api/history?start=${start}&end=${end}&points=${BUCKETS}&raw=${raw ? 1 : 0}`);
      if (request !== historyRequest) return;  // a newer request is under way
      if (!response.ok) throw new Error(response.status === 429 || response.status === 503 ? "the server is busy" : `error ${response.status}`);
      const body = await response.json();
      if (request !== historyRequest) return;
      history = {start, end, raw, gas: body.gas, ...historySeries(body, start, end)};
      historyError = "";
    } catch (e) {
      if (request !== historyRequest) return;
      historyError = e.message || "no answer from the server";
    }
    showHistory();
  }

  function renderHistoryControls() {
    for (const b of byId("ranges").children) b.classList.toggle("on", !zoom && Number(b.dataset.seconds) === rangeSeconds);
    for (const b of byId("views").children) b.classList.toggle("on", (b.dataset.raw === "1") === rawView);
  }

  function showHistory() {
    const available = !meta || meta.history;
    byId("history-controls").hidden = !available;
    byId("history-plot").hidden = !available;
    renderHistoryControls();
    if (historyPlot && available) historyPlot.setData(historyData());
    byId("history-note").textContent = !available ? "History is not available on this server: the store is not installed."
      : historyError ? `The history could not be loaded: ${historyError}.`
      : !history ? ""
      : (history.t.length === 0 ? "No stored readings in this range. " : "")
        + (history.raw ? "Dashed lines are the base-pressure limits. "
                       : "Base-pressure limits apply to the gauge reading; choose \"Gauge reading\" to see them. ")
        + "Dotted lines mark a change of gas. Drag to zoom into a night, double-click to go back.";
  }

  //=============================================================================================================================================
  // Frames

  function onFrame(frame) {
    hostNow = frame.now;
    waiting = frame.waiting;
    ageS = frame.age_s;
    latest = frame.latest;
    gas = frame.gas;
    gasChanges = frame.gas_changes;
    let rebuild = livePlot === null, first = meta === null;
    if (frame.meta) {
      const before = gauges().map(g => g.key).join();
      meta = frame.meta;
      zone = meta.zone || zone;
      rebuild = rebuild || gauges().map(g => g.key).join() !== before;
      renderNotes();
    }
    if (frame.reset || frame.keys.join() !== keys.join()) {
      keys = frame.keys;
      live = {t: [], p: Object.fromEntries(keys.map(key => [key, []]))};
    }
    for (const when of frame.t) live.t.push(when);
    keys.forEach((key, column) => { for (const v of frame.p[column]) live.p[key].push(positive(v)); });
    const cut = live.t.length ? live.t.findIndex(when => when >= live.t[live.t.length - 1] - meta.live_seconds) : 0;
    if (cut > 0) {
      live.t.splice(0, cut);
      for (const key of keys) live.p[key].splice(0, cut);
    }

    renderHeader();
    renderAlarmBar();
    renderGas();
    renderTiles();
    if (rebuild) buildPlots();
    if (!livePaused) showLive();
    if (first || rebuild) {
      showHistory();
      loadHistory();
    }
  }

  function onClosed(note, reason) {
    closedNote = note;
    waiting = true;  // the tiles dim: their numbers are no longer current
    byId("closed").hidden = false;
    byId("closed").textContent = reason ? `${note} (${reason})` : note;
    renderHeader();
    byId("waiting").hidden = true;  // the closed bar says why
    renderTiles();
  }

  //=============================================================================================================================================
  // Start

  for (const [label, seconds] of RANGES) {
    const b = document.createElement("button");
    b.id = `range-${seconds}`;
    b.textContent = label;
    b.dataset.seconds = seconds;
    b.onclick = () => {
      rangeSeconds = seconds;
      zoom = null;
      loadHistory();
    };
    byId("ranges").append(b);
  }
  for (const b of byId("views").children) {
    b.onclick = () => {
      rawView = b.dataset.raw === "1";
      loadHistory();
    };
  }
  renderHistoryControls();
  byId("live-pause").onclick = () => setLivePaused(!livePaused);

  // A range that ends now moves on with the clock. A zoomed range, a hidden tab and a closed page stay as they are.
  setInterval(() => { if (!zoom && !document.hidden && !closedNote) loadHistory(); }, HISTORY_REFRESH_MS);

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!livePlot) return;
      livePlot.setSize({width: byId("live-plot").clientWidth, height: plotHeight()});
      historyPlot.setSize({width: byId("history-plot").clientWidth, height: plotHeight()});
    }, 150);
  });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (livePlot) buildPlots(); });

  const url = new URL("ws", location.href);
  url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  PressureSession.openSession({url, WebSocket, document, timers: window, now: () => Date.now(), onFrame, onClosed});
})();
