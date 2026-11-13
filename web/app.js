(() => {
  const $ = (id) => document.getElementById(id);

  const HIST_VOL_COLOR = "rgba(70, 130, 220, 0.5)";
  const PRED_VOL_COLOR = "rgba(212, 163, 92, 0.4)";
  const EST = "America/New_York";

  const state = {
    symbol: null,
    timeframe: null,
    symbols: [],
    history: [],
    forecast: [],
    liveBars: [],
    overlays: {
      close: true,
      candles: false,
      envelope: true,
      volume: false,
      band: false,
    },
  };

  function formatEstLabel(unixSec) {
    return (
      new Intl.DateTimeFormat("en-US", {
        timeZone: EST,
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(new Date(unixSec * 1000)) + " EST"
    );
  }

  const chartEl = $("chart");
  const chart = LightweightCharts.createChart(chartEl, {
    layout: {
      background: { type: "solid", color: "transparent" },
      textColor: "#8aa396",
      fontFamily: "Sora, system-ui, sans-serif",
    },
    grid: {
      vertLines: { color: "rgba(231,242,236,0.06)" },
      horzLines: { color: "rgba(231,242,236,0.06)" },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: {
      borderColor: "rgba(231,242,236,0.12)",
      autoScale: true,
      scaleMargins: { top: 0.08, bottom: 0.22 },
    },
    timeScale: {
      borderColor: "rgba(231,242,236,0.12)",
      timeVisible: true,
      secondsVisible: false,
      borderVisible: true,
    },
  });

  const candleSeries = chart.addCandlestickSeries({
    upColor: "#3ecf8e",
    downColor: "#e26d5a",
    borderUpColor: "#3ecf8e",
    borderDownColor: "#e26d5a",
    wickUpColor: "#3ecf8e",
    wickDownColor: "#e26d5a",
  });

  const predCandleSeries = chart.addCandlestickSeries({
    upColor: "rgba(212,163,92,0.55)",
    downColor: "rgba(212,163,92,0.35)",
    borderUpColor: "#d4a35c",
    borderDownColor: "#d4a35c",
    wickUpColor: "#d4a35c",
    wickDownColor: "#d4a35c",
    visible: false,
  });

  const predCloseSeries = chart.addLineSeries({
    color: "#d4a35c",
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
  });

  const highSeries = chart.addLineSeries({
    color: "rgba(62,207,142,0.65)",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dotted,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  const lowSeries = chart.addLineSeries({
    color: "rgba(226,109,90,0.65)",
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dotted,
    priceLineVisible: false,
    lastValueVisible: false,
  });

  const bandHiSeries = chart.addLineSeries({
    color: "rgba(212,163,92,0.35)",
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    visible: false,
  });

  const bandLoSeries = chart.addLineSeries({
    color: "rgba(212,163,92,0.35)",
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: false,
    visible: false,
  });

  const volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: "volume" },
    priceScaleId: "vol",
    color: HIST_VOL_COLOR,
  });
  chart.priceScale("vol").applyOptions({
    scaleMargins: { top: 0.82, bottom: 0 },
    borderVisible: false,
  });

  function resize() {
    const width = Math.max(1, chartEl.clientWidth);
    const height = Math.max(1, chartEl.clientHeight);
    chart.applyOptions({ width, height });
  }
  window.addEventListener("resize", resize);
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => resize()).observe(chartEl);
  }
  // After layout settles (flex header height), size the chart once more.
  requestAnimationFrame(() => requestAnimationFrame(resize));
  resize();

  function selectedSymbol() {
    return String($("symbol").value || "").trim().toUpperCase();
  }

  function selectedTimeframe() {
    return $("timeframe").value;
  }

  function selectedUpdateSeconds() {
    return Number($("update-interval").value);
  }

  function matchesSelection(payload) {
    if (!payload) return false;
    const sym = payload.symbol;
    const tf = payload.timeframe != null ? String(payload.timeframe) : null;
    if (sym && sym !== selectedSymbol()) return false;
    if (tf && tf !== String(selectedTimeframe())) return false;
    return true;
  }

  // Use raw UTC unix seconds — do not shift/format times for the series data.
  // Custom timezone munging was blanking / breaking the time scale.
  function toCandle(b) {
    return {
      time: b.time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    };
  }

  function toLinePoint(b, value) {
    return { time: b.time, value };
  }

  function toVolPoint(b, color) {
    return {
      time: b.time,
      value: b.volume || 0,
      color,
    };
  }

  function refitView() {
    chart.priceScale("right").applyOptions({ autoScale: true });
    candleSeries.priceScale().applyOptions({ autoScale: true });
    chart.timeScale().fitContent();
  }

  let appliedRunId = null;
  let fetchLatestSeq = 0;

  async function fetchLatestAndApply(hint = {}) {
    const sym = hint.symbol || selectedSymbol();
    const tf = hint.timeframe != null ? String(hint.timeframe) : selectedTimeframe();
    if (!sym || !tf) return;
    const seq = ++fetchLatestSeq;
    try {
      const res = await fetch(
        `/api/latest?symbol=${encodeURIComponent(sym)}&timeframe=${encodeURIComponent(tf)}`
      );
      const data = await res.json();
      if (seq !== fetchLatestSeq) return; // newer request won
      if (!data || !data.run) return;
      applyForecast({
        run_id: data.run.id,
        symbol: data.run.symbol,
        timeframe: data.run.timeframe,
        origin_bar_time: data.run.origin_bar_time,
        latency_ms: data.run.latency_ms,
        history: data.history,
        forecast: data.forecast,
        live_bars: data.history,
      });
    } catch (err) {
      console.warn("fetchLatestAndApply failed:", err);
    }
  }

  function syncSecondsVisible() {
    const tf = selectedTimeframe();
    const showSeconds = typeof tf === "string" && tf.endsWith("S");
    chart.timeScale().applyOptions({ secondsVisible: showSeconds });
  }

  function render({ refit = false } = {}) {
    const histSource = state.liveBars.length ? state.liveBars : state.history;
    const hist = histSource.map(toCandle);
    candleSeries.setData(hist);

    const fc = state.forecast || [];
    predCloseSeries.setData(
      state.overlays.close ? fc.map((b) => toLinePoint(b, b.close)) : []
    );
    predCandleSeries.applyOptions({ visible: state.overlays.candles });
    predCandleSeries.setData(state.overlays.candles ? fc.map(toCandle) : []);

    highSeries.setData(
      state.overlays.envelope ? fc.map((b) => toLinePoint(b, b.high)) : []
    );
    lowSeries.setData(
      state.overlays.envelope ? fc.map((b) => toLinePoint(b, b.low)) : []
    );

    const bandOn = state.overlays.band && fc.some((b) => b.close_lo != null && b.close_hi != null);
    bandHiSeries.applyOptions({ visible: bandOn });
    bandLoSeries.applyOptions({ visible: bandOn });
    bandHiSeries.setData(bandOn ? fc.map((b) => toLinePoint(b, b.close_hi)) : []);
    bandLoSeries.setData(bandOn ? fc.map((b) => toLinePoint(b, b.close_lo)) : []);

    const volPoints = histSource.map((b) => toVolPoint(b, HIST_VOL_COLOR));
    if (state.overlays.volume) {
      for (const b of fc) volPoints.push(toVolPoint(b, PRED_VOL_COLOR));
    }
    volumeSeries.setData(volPoints);

    if (refit) refitView();
  }

  function setPill(key, text, cls) {
    const el = document.querySelector(`.pill[data-k="${key}"]`);
    if (!el) return;
    el.textContent = text;
    el.classList.remove("ok", "bad", "busy");
    if (cls) el.classList.add(cls);
  }

  function applyStatus(payload) {
    setPill("tv", payload.tv_connected ? "TV · connected" : "TV · offline", payload.tv_connected ? "ok" : "bad");
    const device = payload.device ? String(payload.device) : "";
    setPill(
      "model",
      payload.model_ready ? `Model · ready${device ? " · " + device : ""}` : "Model · loading",
      payload.model_ready ? "ok" : "busy"
    );
    if (payload.predicting) {
      setPill("run", "Run · predicting", "busy");
    } else if (payload.last_error) {
      setPill("run", "Run · error", "bad");
    } else if (payload.last_run_id) {
      setPill("run", `Run · #${payload.last_run_id}`, "ok");
      // Fallback if forecast_ready was missed: pull when run id advances.
      if (payload.last_run_id !== appliedRunId && matchesSelection(payload)) {
        fetchLatestAndApply(payload);
      }
    } else {
      setPill("run", "Run · idle", null);
    }
  }

  function applyForecast(payload) {
    if (!matchesSelection(payload)) return;
    const hist = payload.history || [];
    const fc = payload.forecast || [];
    // Never wipe a visible chart with an empty payload.
    if (!hist.length && !fc.length && (state.history.length || state.liveBars.length)) {
      console.warn("Ignoring empty forecast payload");
      return;
    }
    state.symbol = payload.symbol;
    state.timeframe = payload.timeframe != null ? String(payload.timeframe) : state.timeframe;
    state.history = hist;
    state.forecast = fc;
    state.liveBars = payload.live_bars || hist;
    if (payload.run_id != null) appliedRunId = payload.run_id;
    syncSecondsVisible();
    try {
      render({ refit: true });
    } catch (err) {
      console.warn("render failed:", err);
    }
    $("meta-latency").textContent = payload.latency_ms != null ? `${Math.round(payload.latency_ms)} ms` : "—";
    $("meta-origin").textContent = payload.origin_bar_time
      ? formatEstLabel(payload.origin_bar_time)
      : "—";
    $("meta-run").textContent = payload.run_id != null ? `#${payload.run_id}` : "—";
  }

  const TF_LABELS = {
    "10S": "10 seconds",
    "30S": "30 seconds",
    "1": "1 minute",
    "5": "5 minutes",
    "10": "10 minutes",
    "15": "15 minutes",
    "30": "30 minutes",
    "60": "1 hour",
    "240": "4 hours",
    D: "1 day",
  };

  function renderSymbolMenu() {
    const list = $("symbol-menu-list");
    const empty = $("symbol-menu-empty");
    list.innerHTML = "";
    if (!state.symbols.length) {
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    for (const sym of state.symbols) {
      const row = document.createElement("div");
      row.className = "symbol-item";

      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "pick";
      pick.textContent = sym;
      pick.addEventListener("click", () => {
        $("symbol").value = sym;
        closeSymbolMenu();
        switchInstrument();
      });

      const del = document.createElement("button");
      del.type = "button";
      del.className = "del";
      del.setAttribute("aria-label", `Remove ${sym}`);
      del.textContent = "×";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        await deleteSymbol(sym);
      });

      row.appendChild(pick);
      row.appendChild(del);
      list.appendChild(row);
    }
  }

  function openSymbolMenu() {
    $("symbol-menu").hidden = false;
    $("symbol-combo").classList.add("open");
    $("symbol-toggle").setAttribute("aria-expanded", "true");
    renderSymbolMenu();
  }

  function closeSymbolMenu() {
    $("symbol-menu").hidden = true;
    $("symbol-combo").classList.remove("open");
    $("symbol-toggle").setAttribute("aria-expanded", "false");
  }

  function toggleSymbolMenu() {
    if ($("symbol-menu").hidden) openSymbolMenu();
    else closeSymbolMenu();
  }

  async function deleteSymbol(sym) {
    const res = await fetch(`/api/symbols/${encodeURIComponent(sym)}`, { method: "DELETE" });
    if (!res.ok) return;
    const data = await res.json();
    state.symbols = data.symbols || [];
    renderSymbolMenu();
  }

  async function loadConfig() {
    const res = await fetch("/api/config");
    const cfg = await res.json();
    state.symbols = cfg.symbols || [];
    $("symbol").value = cfg.defaults.symbol || "ES1!";

    const tf = $("timeframe");
    tf.innerHTML = "";
    for (const t of cfg.timeframes) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = TF_LABELS[t] || t;
      if (t === cfg.defaults.timeframe) opt.selected = true;
      tf.appendChild(opt);
    }

    const upd = $("update-interval");
    upd.innerHTML = "";
    const intervals = cfg.update_intervals || [60, 300, 600];
    const currentUpdate = Number(cfg.defaults.update_seconds || 60);
    for (const sec of intervals) {
      const opt = document.createElement("option");
      opt.value = String(sec);
      const mins = Number(sec) / 60;
      opt.textContent = mins === 1 ? "1 minute" : `${mins} minutes`;
      if (Number(sec) === currentUpdate) opt.selected = true;
      upd.appendChild(opt);
    }

    state.symbol = cfg.defaults.symbol;
    state.timeframe = String(cfg.defaults.timeframe);
    $("temperature").value = cfg.defaults.temperature;
    $("top_p").value = cfg.defaults.top_p;
    $("sample_count").value = cfg.defaults.sample_count;
    $("band_samples").value = cfg.defaults.band_samples;
    syncSecondsVisible();
    renderSymbolMenu();
  }

  async function configure(extra = {}) {
    const body = {
      symbol: selectedSymbol(),
      timeframe: selectedTimeframe(),
      update_seconds: selectedUpdateSeconds(),
      temperature: Number($("temperature").value),
      top_p: Number($("top_p").value),
      sample_count: Number($("sample_count").value),
      band_samples: Number($("band_samples").value),
      force: true,
      ...extra,
    };
    const res = await fetch("/api/configure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    const data = await res.json();
    if (data.symbols) {
      state.symbols = data.symbols;
      renderSymbolMenu();
    }
    return data;
  }

  async function switchInstrument() {
    const sym = selectedSymbol();
    if (!sym) {
      setPill("run", "Run · need symbol", "bad");
      return;
    }
    $("symbol").value = sym;
    state.symbol = sym;
    state.timeframe = selectedTimeframe();
    // Keep previous series visible until the new forecast arrives (no blank flash).
    syncSecondsVisible();
    setPill("run", "Run · switching", "busy");
    try {
      await configure();
    } catch (err) {
      setPill("run", "Run · error", "bad");
      console.warn("configure failed:", err);
    }
  }

  function bindOverlays() {
    const map = {
      "ov-close": "close",
      "ov-candles": "candles",
      "ov-envelope": "envelope",
      "ov-volume": "volume",
      "ov-band": "band",
    };
    for (const [id, key] of Object.entries(map)) {
      $(id).addEventListener("change", (e) => {
        state.overlays[key] = e.target.checked;
        render();
      });
    }
  }

  let socket = null;
  let pingTimer = null;

  function connectWs() {
    if (pingTimer) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      try {
        socket.close();
      } catch {
        /* ignore */
      }
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    socket = ws;
    ws.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (err) {
        console.warn("Bad WS JSON (update dropped):", err);
        return;
      }
      if (msg.type === "status") applyStatus(msg.payload || {});
      if (msg.type === "forecast_ready") {
        if (!matchesSelection(msg.payload || {})) return;
        fetchLatestAndApply(msg.payload || {});
      }
      // Back-compat if an older server still pushes full forecasts
      if (msg.type === "forecast") applyForecast(msg.payload || {});
      if (msg.type === "bars") {
        if (!matchesSelection(msg.payload)) return;
        const bars = (msg.payload && msg.payload.bars) || [];
        if (!bars.length) return;
        const wasEmpty = !(state.liveBars.length || state.history.length);
        state.liveBars = bars;
        try {
          render({ refit: wasEmpty });
        } catch (err) {
          console.warn("bars render failed:", err);
        }
      }
      if (msg.type === "error") {
        setPill("run", "Run · error", "bad");
        console.warn("Diviner error:", msg.payload);
      }
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      setPill("tv", "TV · reconnecting", "busy");
      setTimeout(connectWs, 2000);
    };
    pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
    }, 25000);
  }

  $("symbol").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      closeSymbolMenu();
      switchInstrument();
    } else if (e.key === "Escape") {
      closeSymbolMenu();
    }
  });
  $("symbol-toggle").addEventListener("click", (e) => {
    e.preventDefault();
    toggleSymbolMenu();
  });
  document.addEventListener("click", (e) => {
    if (!$("symbol-combo").contains(e.target)) closeSymbolMenu();
  });

  $("timeframe").addEventListener("change", () => switchInstrument());
  $("update-interval").addEventListener("change", async () => {
    try {
      await configure({ force: false });
    } catch (err) {
      console.warn("update interval failed:", err);
    }
  });
  $("refresh").addEventListener("click", async () => {
    await switchInstrument();
    await fetch("/api/refresh", { method: "POST" });
  });
  $("apply-params").addEventListener("click", async () => {
    await configure();
  });
  $("draw-tv").addEventListener("click", async () => {
    const btn = $("draw-tv");
    if (btn.disabled) return;
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "Drawing…";
    let ok = false;
    let segments = null;
    try {
      const body = {
        symbol: selectedSymbol(),
        timeframe: selectedTimeframe(),
      };
      if (appliedRunId != null) body.run_id = appliedRunId;
      const res = await fetch("/api/draw-prediction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = data.detail;
        const msg =
          typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((d) => d.msg || JSON.stringify(d)).join("; ")
              : res.statusText;
        throw new Error(msg || "Draw failed");
      }
      ok = true;
      segments = data.segments;
    } catch (err) {
      console.warn("draw-tv failed:", err);
      setPill("run", "Run · draw error", "bad");
      alert(err.message || String(err));
    } finally {
      if (ok) {
        btn.textContent = `Drawn · ${segments != null ? segments : "?"} segs`;
        setTimeout(() => {
          btn.textContent = prev;
          btn.disabled = false;
        }, 2000);
      } else {
        btn.textContent = prev;
        btn.disabled = false;
      }
    }
  });

  bindOverlays();
  loadConfig()
    .then(() => fetchLatestAndApply())
    .catch(console.error)
    .finally(connectWs);
})();
