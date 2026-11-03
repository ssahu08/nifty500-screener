"use strict";

const IST = "Asia/Kolkata";
const REFRESH_MS = 30000;
//: Signals shown per strategy before the "Show all" toggle.
const PREVIEW_ROWS = 5;

const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const meta = document.getElementById("meta");
const overviewEl = document.getElementById("overview");
const overviewErrors = document.getElementById("overview-errors");
const overviewUpdated = document.getElementById("overview-updated");
const resultsEl = document.getElementById("results");
const resultsUpdated = document.getElementById("results-updated");
const filtersEl = document.getElementById("filters");
const toastsEl = document.getElementById("toasts");
const adminToggle = document.getElementById("admin-toggle");
const adminPanel = document.getElementById("admin-panel");
const refreshAdminBtn = document.getElementById("refresh-admin");
const adminUpdated = document.getElementById("admin-updated");
const overviewToggle = document.getElementById("overview-toggle");
const overviewPanel = document.getElementById("overview-panel");
const hedge23Body = document.getElementById("hedge23-body");
const hedge23Status = document.getElementById("hedge23-status");
const hedge23Updated = document.getElementById("hedge23-updated");
const refreshHedge23Btn = document.getElementById("refresh-hedge23");

const timeFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST,
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const dayFmt = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short" });
const istHmFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: IST,
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const numFmt = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function chartUrl(symbol) {
  // TradingView chart for NSE symbol — no API key or auth needed.
  return `https://in.tradingview.com/chart/?symbol=NSE:${encodeURIComponent(symbol)}`;
}

// -- scan sound effects (Web Audio API, no external files) ------------------
let audioCtx;
function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  return audioCtx;
}
function playTone(freq, duration = 0.15, type = "sine", vol = 0.12) {
  try {
    const ctx = ensureAudio();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, ctx.currentTime);
    osc.connect(gain);
    gain.connect(ctx.destination);
    gain.gain.setValueAtTime(vol, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duration);
    osc.start();
    osc.stop(ctx.currentTime + duration);
  } catch {
    /* audio is optional; silent failure is fine */
  }
}
function playScanStart() {
  playTone(520, 0.12, "sine");
  setTimeout(() => playTone(660, 0.18, "sine"), 120);
}
function playScanDone(signalsFound) {
  if (signalsFound) {
    playTone(440, 0.15, "sine");
    setTimeout(() => playTone(560, 0.15, "sine"), 150);
    setTimeout(() => playTone(700, 0.3, "sine"), 300);
  } else {
    playTone(320, 0.25, "sine");
    setTimeout(() => playTone(260, 0.35, "sine"), 250);
  }
}
// Track sounds already played per run so we don't replay on every poll.
const playedSounds = new Set();

const state = {
  strategies: [],
  results: new Map(),
  filter: "all",
  overview: null,
  expanded: new Set(),
  runs: new Map(),
  modalName: null,
  subtabs: new Map(),
  minimizedRuns: new Set(),
  hedge23: null,
};

const adminState = {
  usage: null,
  storage: null,
};

// -- formatting helpers -----------------------------------------------------
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const hm = istHmFmt.format(d);
  if (hm === "05:30" || hm === "00:00") {
    // Date-only period labels: daily bars (05:30 IST from FYERS) and
    // resampled weekly/monthly bars (00:00 IST).
    return timeFmt
      .formatToParts(d)
      .filter((p) => p.type === "day" || p.type === "month")
      .map((p) => p.value)
      .join(" ");
  }
  return timeFmt.format(d).replace(",", "");
}

function fmtDay(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return isNaN(d) ? iso : dayFmt.format(d);
}

function fmtPrice(value) {
  return value == null ? "—" : numFmt.format(Number(value));
}

function toast(message, isError = false) {
  const el = document.createElement("div");
  el.className = `toast${isError ? " err" : ""}`;
  el.textContent = message;
  toastsEl.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

async function getJson(url, options) {
  const res = await fetch(url, { cache: "no-store", ...options });
  if (res.status === 401) {
    showPinGate("Enter the dashboard PIN to continue");
    const err = new Error("PIN required");
    err.pinRequired = true;
    throw err;
  }
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// -- PIN gate (public deployments) ------------------------------------------
const pinGateEl = document.getElementById("pin-gate");
const pinInputEl = document.getElementById("pin-input");
const pinErrorEl = document.getElementById("pin-error");
const pinSubmitEl = document.getElementById("pin-submit");

function pinGateVisible() {
  return Boolean(pinGateEl) && !pinGateEl.classList.contains("hidden");
}

function showPinGate(message = "") {
  if (!pinGateEl) return;
  pinGateEl.classList.remove("hidden");
  if (message && pinErrorEl) pinErrorEl.textContent = message;
  if (pinInputEl) {
    pinInputEl.value = "";
    pinInputEl.focus();
  }
}

function hidePinGate() {
  if (pinGateEl) pinGateEl.classList.add("hidden");
}

async function authNeeded() {
  try {
    const status = await getJson("/api/auth/status");
    return Boolean(status.enabled && !status.authenticated);
  } catch {
    return false; // let data loads surface the real error
  }
}

async function submitPin() {
  const pin = (pinInputEl?.value || "").trim();
  if (!pin) {
    pinErrorEl.textContent = "Enter your PIN";
    return;
  }
  pinSubmitEl.disabled = true;
  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      pinErrorEl.textContent = body.detail || "Incorrect PIN";
      pinInputEl.select();
      return;
    }
    pinInputEl.value = "";
    pinErrorEl.textContent = "";
    hidePinGate();
    toast("Unlocked");
    await init();
  } catch {
    pinErrorEl.textContent = "Network error — try again";
  } finally {
    pinSubmitEl.disabled = false;
  }
}

pinSubmitEl?.addEventListener("click", submitPin);
pinInputEl?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") submitPin();
});

// -- health -----------------------------------------------------------------
async function checkHealth() {
  try {
    const data = await getJson("/health");
    statusDot.className = "status-dot ok";
    statusText.textContent = "api online";
    meta.textContent = `${data.app} · env=${data.env} · retention=${data.retention_days}d`;
  } catch (err) {
    statusDot.className = "status-dot err";
    statusText.textContent = "api offline";
    meta.textContent = String(err);
  }
}

// -- market overview --------------------------------------------------------
function renderOverview(data) {
  state.overview = data;
  overviewUpdated.textContent = `updated ${new Date().toLocaleTimeString("en-GB")}`;

  const errors = data.errors || [];
  if (errors.length) {
    overviewErrors.textContent = errors.map((e) => `! ${e}`).join("   ");
    overviewErrors.hidden = false;
  } else {
    overviewErrors.hidden = true;
  }

  const head = ["Index", ...data.columns.flatMap((d) => [fmtDay(d), "Change %"])]
    .map((label) => `<th>${escapeHtml(label)}</th>`)
    .join("");

  const body = data.rows
    .map((row) => {
      const cls = row.label === "Nifty" ? ' class="nifty"' : "";
      const cells = row.values
        .map((v, i) => {
          if (v == null) {
            return '<td class="closed">Closed</td><td class="closed">—</td>';
          }
          const chg = row.changes[i];
          if (chg == null) {
            return `<td>${fmtPrice(v)}</td><td class="chg muted">—</td>`;
          }
          const sign = chg >= 0 ? "pos" : "neg";
          const text = `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%`;
          return `<td>${fmtPrice(v)}</td><td class="chg ${sign}">${text}</td>`;
        })
        .join("");
      return `<tr${cls}><td>${escapeHtml(row.label)}</td>${cells}</tr>`;
    })
    .join("");

  overviewEl.innerHTML = `<div class="table-wrap"><table class="grid">
    <thead><tr>${head}</tr></thead>
    <tbody>${body}</tbody>
  </table></div>`;
}

async function loadOverview(refresh = false) {
  overviewEl.innerHTML = '<div class="placeholder">Loading market overview…</div>';
  try {
    renderOverview(await getJson(`/api/overview${refresh ? "?refresh=true" : ""}`));
  } catch (err) {
    overviewEl.innerHTML = `<div class="placeholder">Overview unavailable — ${escapeHtml(err.message)}</div>`;
  }
}

// -- strategy results -------------------------------------------------------
function bandPill(meta) {
  const band = meta && meta.band;
  if (!band) return "";
  const cls = band === "BUY" ? "buy" : band === "HOLD" ? "hold" : "skip";
  return ` <span class="band-pill ${cls}">${escapeHtml(band)}</span>`;
}

function dirPill(meta) {
  const direction = meta && meta.direction;
  if (!direction) return "";
  const cls = direction === "SHORT" ? "short" : "long";
  return `<span class="dir-pill ${cls}">${escapeHtml(direction)}</span> `;
}

function qualityPill(meta) {
  const quality = meta && meta.quality;
  if (!quality) return "";
  const cls = quality === "HIGH" ? "high" : quality === "MEDIUM" ? "medium" : "low";
  return ` <span class="quality-pill ${cls}">${escapeHtml(quality)}</span>`;
}

function volumeTooltip(meta) {
  const parts = [];
  if (meta.open_vol_ratio != null) {
    const days = meta.open_vol_history_days || 10;
    parts.push(`09:15 bar ${meta.open_vol_ratio}x vs prior ${days} sessions`);
    if (meta.open_vol_pctile != null) {
      parts.push(`beats ${meta.open_vol_pctile}% of those sessions`);
    }
    if (meta.open_vol_vs_prev != null) {
      parts.push(`${meta.open_vol_vs_prev}x vs yesterday's opening bar`);
    }
  } else if (meta.daily_vol_ratio != null) {
    parts.push("10m history unavailable - using daily volume");
  }
  if (meta.daily_vol_ratio != null) {
    parts.push(`full-day volume ${meta.daily_vol_ratio}x vs 20d average`);
  }
  if (meta.first_bar_turnover_cr != null) {
    parts.push(`opening turnover ₹${meta.first_bar_turnover_cr}cr`);
  }
  if (meta.volume_basis) parts.push(`basis ${meta.volume_basis}`);
  return parts.join(" · ");
}

function scoreTooltip(meta) {
  const parts = [];
  if (meta.direction) parts.push(meta.direction);
  if (meta.setup_type) parts.push(meta.setup_type);
  if (meta.entry != null) parts.push(`entry ${numFmt.format(meta.entry)}`);
  if (meta.stop != null) parts.push(`SL ${numFmt.format(meta.stop)}`);
  if (meta.risk_pct != null) parts.push(`risk ${meta.risk_pct}%`);
  if (meta.rsi9 != null) parts.push(`RSI9 ${meta.rsi9}`);
  if (meta.par20 != null) parts.push(`PAR ${meta.par20}`);
  if (meta.box_range_pct != null) parts.push(`box ${meta.box_range_pct}%`);
  if (meta.squeeze_ratio != null) parts.push(`squeeze ${meta.squeeze_ratio}`);
  if (meta.base_days != null) parts.push(`base ${meta.base_days}d`);
  if (meta.turnover_cr != null) parts.push(`turnover ₹${meta.turnover_cr}cr`);
  // OEL volume-quality details.
  if (meta.open_vol_ratio != null) {
    const days = meta.open_vol_history_days || 10;
    parts.push(`open vol ${meta.open_vol_ratio}x vs ${days}d`);
  }
  if (meta.open_vol_pctile != null) {
    parts.push(`beats ${meta.open_vol_pctile}% of days`);
  }
  if (meta.daily_vol_ratio != null) parts.push(`day vol ${meta.daily_vol_ratio}x`);
  if (meta.first_bar_turnover_cr != null) {
    parts.push(`open turnover ₹${meta.first_bar_turnover_cr}cr`);
  }
  const sp = meta.score_parts;
  if (sp) {
    parts.push(
      `parts vol ${sp.volume} · move ${sp.move} · close ${sp.close} · liq ${sp.liquidity}`
    );
  }
  return parts.join(" · ");
}

function signalsTable(signals, name) {
  if (!signals.length) {
    const message =
      name === "Nifty Hedge 23"
        ? "No entry/exit signal today yet. The live tentative strike is in the Nifty Hedge 23 panel above."
        : "No matches in the latest run.";
    return `<div class="card-table"><div class="empty">${message}</div></div>`;
  }
  const hasScore = signals.some((s) => s.metadata && s.metadata.score != null);
  // Open-Equals-Low: expose the day open/low so the criterion is verifiable.
  const hasDayOpen = signals.some((s) => s.metadata && s.metadata.day_open != null);
  // OEL volume proof: opening 09:15 bar vs prior sessions, plus full-day ratio.
  const hasVolume = signals.some(
    (s) =>
      s.metadata &&
      (s.metadata.open_vol_ratio != null || s.metadata.daily_vol_ratio != null)
  );
  const ordered = hasScore
    ? [...signals].sort(
        (a, b) =>
          (b.metadata.score ?? 0) - (a.metadata.score ?? 0) ||
          (b.metadata.open_vol_ratio ?? 0) - (a.metadata.open_vol_ratio ?? 0)
      )
    : signals;
  // OEL is a short day-level list; show all matches, not just the top five.
  const previewRows = name === "Open-Equals-Low" ? 20 : PREVIEW_ROWS;
  const expanded = state.expanded.has(name);
  const shown = expanded ? ordered : ordered.slice(0, previewRows);
  const rows = shown
    .map((s) => {
      const meta = s.metadata || {};
      const scoreCell = hasScore
        ? `<td class="score-cell" title="${escapeHtml(scoreTooltip(meta))}">${dirPill(meta)}${
            meta.score != null ? meta.score : "—"
          }${qualityPill(meta)}${bandPill(meta)}</td>`
        : "";
      let oelCell = "";
      if (hasDayOpen) {
        const open = meta.day_open;
        const low = meta.day_low;
        const matches = open != null && low != null && open === low;
        const tip = [
          meta.timeframe ? `tf=${meta.timeframe}` : "",
          meta.open_vol_ratio != null
            ? `vol10=${meta.open_vol_ratio}x`
            : meta.volume_ratio != null
              ? `vol=${meta.volume_ratio}x`
              : "",
          meta.open_vol_pctile != null ? `beats ${meta.open_vol_pctile}%` : "",
          meta.move_pct != null ? `move=${meta.move_pct}%` : "",
        ].filter(Boolean).join(" · ");
        oelCell = `<td class="oel-cell${matches ? " match" : ""}" title="${escapeHtml(tip)}">${
          open != null ? fmtPrice(open) : "—"
        } / ${low != null ? fmtPrice(low) : "—"}${matches ? " ✓" : ""}</td>`;
      }
      let volCell = "";
      if (hasVolume) {
        const openRatio = meta.open_vol_ratio;
        const dailyRatio = meta.daily_vol_ratio;
        const primary = openRatio != null ? openRatio : dailyRatio;
        const tag = openRatio != null ? "open" : "day";
        const tone =
          primary == null
            ? ""
            : primary >= 1.5
              ? " strong"
              : primary >= 1.0
                ? " ok"
                : " weak";
        const sub =
          openRatio != null && dailyRatio != null
            ? `<span class="vol-sub">day ${dailyRatio}x</span>`
            : "";
        volCell = `<td class="vol-cell${tone}" title="${escapeHtml(volumeTooltip(meta))}">
          <span class="vol-main">${primary != null ? `${primary}x` : "—"}</span><span class="vol-tag">${tag}</span>${sub}
        </td>`;
      }
      return `<tr>
        <td>${escapeHtml(s.symbol)}</td>
        ${scoreCell}
        ${oelCell}
        ${volCell}
        <td><a href="${chartUrl(s.symbol)}" target="_blank" rel="noopener noreferrer" class="chart-btn">Chart</a></td>
        <td>${escapeHtml(fmtTime(s.signal_time))}</td>
        <td>${fmtPrice(s.price)}</td>
      </tr>`;
    })
    .join("");
  const scoreHead = hasScore ? "<th>Score</th>" : "";
  const oelHead = hasDayOpen ? "<th>Open / Low</th>" : "";
  const volHead = hasVolume
    ? '<th title="First line: 09:15 10-min volume vs the same bar on the prior 10 sessions. Second line: full-day volume vs its 20-day average.">Volume</th>'
    : "";
  const toggle =
    ordered.length > previewRows
      ? `<div class="table-toggle"><button class="btn ghost expand-btn" data-name="${escapeHtml(name)}">${
          expanded ? `Show top ${previewRows}` : `Show all ${ordered.length} signals`
        }</button></div>`
      : "";
  return `<div class="card-table"><table class="grid">
    <thead><tr><th>Symbol</th>${scoreHead}${oelHead}${volHead}<th>Chart</th><th>Signal time</th><th>Price (₹)</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>${toggle}</div>`;
}

function historyTable(signals) {
  if (!signals.length) {
    return '<div class="card-table"><div class="empty">No BUY-band signals in the last 14 days.</div></div>';
  }
  const hasDirection = signals.some((s) => (s.metadata || {}).direction);
  const hasSetup = signals.some((s) => (s.metadata || {}).setup_type);
  const rows = signals
    .map((s) => {
      const meta = s.metadata || {};
      return `<tr>
        <td>${escapeHtml(fmtTime(s.run_at))}</td>
        <td>${escapeHtml(s.symbol)}</td>
        ${hasDirection ? `<td>${dirPill(meta)}</td>` : ""}
        ${hasSetup ? `<td>${escapeHtml(meta.setup_type || "—")}</td>` : ""}
        <td><a href="${chartUrl(s.symbol)}" target="_blank" rel="noopener noreferrer" class="chart-btn">Chart</a></td>
        <td>${escapeHtml(fmtTime(s.signal_time))}</td>
        <td>${fmtPrice(s.price)}</td>
      </tr>`;
    })
    .join("");
  const directionHead = hasDirection ? "<th>Direction</th>" : "";
  const setupHead = hasSetup ? "<th>Setup</th>" : "";
  return `<div class="card-table"><table class="grid">
    <thead><tr><th>Run at</th><th>Symbol</th>${directionHead}${setupHead}<th>Chart</th><th>Signal time</th><th>Price (₹)</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function strategyCard(strategy) {
  const isHedge23 = strategy.name === "Nifty Hedge 23";
  let allSignals = state.results.get(strategy.name) || [];
  // Hedge 23 is an expiry-day strategy: never show a previous expiry's
  // entry/exit as if it were today's. The dedicated panel above carries the
  // live tentative strike even when no signal has fired yet.
  if (isHedge23) {
    const today = state.hedge23 && state.hedge23.today;
    if (today) {
      allSignals = allSignals.filter((s) => String(s.signal_time || "").startsWith(today));
    }
  }
  // Banded strategies are actionable-only: never surface stale HOLD/SKIP
  // rows left in history by an older strategy version.
  const banded = allSignals.some((s) => (s.metadata || {}).band);
  const visibleSignals = banded
    ? allSignals.filter((s) => (s.metadata || {}).band === "BUY")
    : allSignals;
  const activeSubtab = state.subtabs.get(strategy.name) || "all";
  // Filter signals by subtab variant
  const signals =
    activeSubtab === "all"
      ? visibleSignals
      : visibleSignals.filter((s) => (s.metadata || {}).variant === activeSubtab);
  const count = signals.length;
  const lastRun = isHedge23
    ? state.hedge23 && state.hedge23.updated_at
      ? `snapshot ${fmtTime(state.hedge23.updated_at)}`
      : "never run"
    : allSignals.length
      ? fmtTime(allSignals[0].run_at)
      : "never run";
  const sched =
    strategy.schedule_type === "auto"
      ? strategy.run_at
        ? `daily · ${strategy.run_at}`
        : `auto · ${strategy.frequency_minutes || "?"}m`
      : "manual";
  const name = escapeHtml(strategy.name);
  const run = state.runs.get(strategy.name);
  const isRunning = run && run.status.state === "running";
  const runButton = isRunning
    ? `<button class="btn run-btn" data-name="${name}" disabled><span class="spinner"></span>Running <span data-role="btn-time">${fmtElapsed(runElapsed(run))}</span></button>`
    : `<button class="btn run-btn" data-name="${name}">Run now</button>`;

  // Sub-tab buttons for OEL strategy variants
  const subtabButtons =
    strategy.name === "Open-Equals-Low"
      ? `<div class="subtabs">
          <button class="subtab-chip${activeSubtab === "all" ? " active" : ""}" data-name="${name}" data-subtab="all">All</button>
          <button class="subtab-chip${activeSubtab === "intraday" ? " active" : ""}" data-name="${name}" data-subtab="intraday">Intraday (10m)</button>
          <button class="subtab-chip${activeSubtab === "eod" ? " active" : ""}" data-name="${name}" data-subtab="eod">Daily (EOD)</button>
        </div>`
      : "";

  return `<article class="strategy-card${isRunning ? " running" : ""}" data-name="${name}" data-category="${escapeHtml(strategy.category)}">
    <div class="card-head">
      <div class="card-title">
        <h3>${name}</h3>
        <span class="cat-badge ${escapeHtml(strategy.category)}">${escapeHtml(strategy.category)}</span>
        <span class="sched-badge">${escapeHtml(sched)}</span>
        <span class="count-pill${count ? " hit" : ""}">${count} signal${count === 1 ? "" : "s"}</span>
      </div>
      <div class="card-actions">
        ${runButton}
        <button class="btn ghost history-btn" data-name="${name}">History</button>
      </div>
    </div>
    <p class="card-desc">${escapeHtml(strategy.description)}</p>
    ${subtabButtons}
    <div class="card-meta">last run ${escapeHtml(lastRun)} · universe ${escapeHtml(strategy.universe)} · timeframes ${escapeHtml((strategy.timeframes || []).join(", "))}</div>
    ${signalsTable(signals, strategy.name)}
    <div class="history-block hidden"></div>
  </article>`;
}

function renderResults() {
  const visible =
    state.filter === "all"
      ? state.strategies
      : state.strategies.filter((s) => s.category === state.filter);

  if (!visible.length) {
    resultsEl.innerHTML = '<div class="placeholder">No strategies in this category yet.</div>';
    return;
  }
  resultsEl.innerHTML = visible.map(strategyCard).join("");
}

async function loadResults() {
  if (pinGateVisible()) return;
  const [strategies, results] = await Promise.all([
    getJson("/api/strategies"),
    getJson("/api/results"),
  ]);
  state.strategies = strategies;
  state.results = new Map(results.map((r) => [r.strategy, r.signals]));
  resultsUpdated.textContent = `updated ${new Date().toLocaleTimeString("en-GB")}`;
  renderResults();
}

// -- Nifty Hedge 23 panel ---------------------------------------------------
const HEDGE23_STATUS = {
  not_started: ["not started", ""],
  disabled: ["disabled", ""],
  not_expiry_day: ["not expiry day", ""],
  market_closed: ["market closed", ""],
  waiting_entry_window: ["waiting · 13:00", "warn"],
  waiting_zone: ["waiting for zone", "warn"],
  range_bound_wait: ["waiting for trend", "warn"],
  entry_zone: ["entry zone", "ok"],
  position_open: ["position open", "ok"],
  exited: ["exited", ""],
  data_unavailable: ["data unavailable", "danger"],
};

function fmtInr(value) {
  return value == null || Number.isNaN(Number(value))
    ? "—"
    : `₹${numFmt.format(Number(value))}`;
}

function hedgeStat(label, value, { sub = "", tone = "" } = {}) {
  return `<div class="hedge-stat">
    <div class="hedge-stat-label">${escapeHtml(label)}</div>
    <div class="hedge-stat-value${tone ? ` ${tone}` : ""}">${escapeHtml(value)}</div>
    ${sub ? `<div class="hedge-stat-sub">${escapeHtml(sub)}</div>` : ""}
  </div>`;
}

function renderHedge23(data) {
  if (!hedge23Body) return;
  state.hedge23 = data || null;
  if (!data || data.status === "not_started") {
    hedge23Status.textContent = "not started";
    hedge23Status.className = "hedge-status-pill";
    hedge23Updated.textContent = "waiting for the first scan";
    hedge23Body.innerHTML =
      '<div class="placeholder">The strategy has not produced a snapshot yet. It refreshes every 5 minutes during market hours.</div>';
    return;
  }

  const [label, tone] = HEDGE23_STATUS[data.status] || [data.status || "unknown", ""];
  hedge23Status.textContent = label;
  hedge23Status.className = `hedge-status-pill${tone ? ` ${tone}` : ""}`;

  const expiryText = data.expiry_label
    ? `expiry ${data.expiry_label}${data.expiry_kind ? ` (${data.expiry_kind})` : ""}`
    : "";
  hedge23Updated.textContent = [
    data.updated_at ? `updated ${fmtTime(data.updated_at)}` : "",
    expiryText,
    data.age_seconds != null ? `age ${Math.round(data.age_seconds)}s` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const mode = data.mode || "simple";
  const zone = data.entry_zone || {};
  const pos = data.position || {};
  const movement = data.movement || {};
  const combined = data.combined_premium;
  const inZone = Boolean(data.in_entry_zone);
  const recommended = Number(data.recommended_lots || 0);
  const behaviour = data.nifty_behaviour || movement.behaviour || "—";
  const bias = movement.bias || "—";
  const dayRange = movement.day_range_pts;
  const moveOpen = movement.move_from_open_pts;
  const isTrending = movement.is_trending !== false;
  const radius = Number(data.radius_pts || 0);
  const ceStrike = data.ce_strike || data.atm_strike;
  const peStrike = data.pe_strike || data.atm_strike;
  const slPrice = data.sl_price;
  const target1 = data.target_1;
  const target2 = data.target_2;
  const balanced = Boolean(data.legs_balanced);

  const movementSub = [
    bias !== "—" ? bias.toLowerCase() : "",
    dayRange != null ? `range ${dayRange} pts` : "",
    moveOpen != null ? `from open ${moveOpen >= 0 ? "+" : ""}${moveOpen}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const stats = [
    hedgeStat("Nifty spot", data.spot != null ? numFmt.format(data.spot) : "—"),
    hedgeStat("Movement", behaviour, {
      tone: !isTrending ? "amber" : inZone ? "accent" : "",
      sub: movementSub || "expiry behaviour",
    }),
    hedgeStat(
      "ATM strike",
      data.atm_strike ? numFmt.format(data.atm_strike) : "—",
      { sub: radius ? `${radius}-pt radius` : "ATM (0-pt radius)" }
    ),
    hedgeStat("Combined CE+PE", combined != null ? fmtInr(combined) : "—", {
      tone: inZone ? "accent" : "",
      sub: inZone
        ? balanced
          ? "inside entry zone · legs balanced"
          : "inside entry zone · legs unbalanced"
        : "outside entry zone",
    }),
    hedgeStat("Entry zone", `${zone.low ?? "—"}–${zone.high ?? "—"}`, {
      sub: `extended ${zone.extended_low ?? "—"}–${zone.extended_high ?? "—"}`,
    }),
    hedgeStat("Stop / targets", slPrice != null ? fmtInr(slPrice) : "—", {
      tone: "amber",
      sub: `T1 ${fmtInr(target1)} · T2 ${fmtInr(target2)}`,
    }),
    hedgeStat("Mode", mode, {
      sub: mode === "averaging" ? "50-30-20 ladder" : "single entry",
    }),
    hedgeStat(
      "Recommended",
      recommended ? `${recommended} lot${recommended === 1 ? "" : "s"}` : "wait",
      {
        sub: recommended
          ? fmtInr(data.recommended_cost)
          : `1 lot ${fmtInr(data.cost_per_lot)}`,
      }
    ),
  ].join("");

  let legsHtml;
  if (data.is_expiry_day && data.ce_symbol && data.pe_symbol) {
    const heading = pos.open
      ? "Open position"
      : !isTrending
        ? "Tentative strike — Nifty range-bound"
        : inZone
          ? "Tentative buy — in entry zone"
          : "Tentative strike — waiting for the zone";
    const lotSize = data.lot_size ?? "—";
    legsHtml = `<div class="hedge-tentative${inZone || pos.open ? "" : " waiting"}">
      <h3>${escapeHtml(heading)}</h3>
      <table class="hedge-legs">
        <thead><tr><th>Leg</th><th>Strike</th><th>Fyers symbol</th><th>Premium</th><th>Qty / lot</th></tr></thead>
        <tbody>
          <tr>
            <td><span class="leg-side ce">CE</span></td>
            <td>${fmtPrice(ceStrike)}</td>
            <td class="leg-symbol">${escapeHtml(data.ce_symbol)}</td>
            <td>${data.ce_premium != null ? fmtInr(data.ce_premium) : "—"}</td>
            <td>${escapeHtml(lotSize)}</td>
          </tr>
          <tr>
            <td><span class="leg-side pe">PE</span></td>
            <td>${fmtPrice(peStrike)}</td>
            <td class="leg-symbol">${escapeHtml(data.pe_symbol)}</td>
            <td>${data.pe_premium != null ? fmtInr(data.pe_premium) : "—"}</td>
            <td>${escapeHtml(lotSize)}</td>
          </tr>
        </tbody>
      </table>
      <div class="hedge-note" style="margin-top:8px">
        Buy both legs together as one ${radius ? `${radius}-pt radius strangle` : "ATM straddle"}.
        ${
          recommended
            ? `Recommended ${recommended} lot${recommended === 1 ? "" : "s"} ≈ ${fmtInr(data.recommended_cost)} at the current combined premium.`
            : "No even lot fits the capital cap at this premium — wait or raise the configured capital."
        }
      </div>
      ${
        !balanced
          ? '<div class="hedge-note warn" style="margin-top:6px">CE and PE premiums are not balanced (needs within 15 pts or ≤ 2.2x) — entry is skipped until they balance.</div>'
          : ""
      }
      ${
        !isTrending
          ? `<div class="hedge-note warn" style="margin-top:6px">Nifty is ${escapeHtml(behaviour)}; the strategy waits for a trending move or an afternoon compression coil before entry.</div>`
          : ""
      }
    </div>`;
  } else if (!data.is_expiry_day) {
    legsHtml = `<div class="hedge-note">Today is not a Nifty expiry day. Next expiry: <strong>${escapeHtml(data.expiry_label || "—")}</strong>.</div>`;
  } else {
    legsHtml = `<div class="hedge-note warn">Option quotes are unavailable right now. ${escapeHtml(
      data.error || "The market may be closed or the Fyers token may have expired."
    )}</div>`;
  }

  let positionHtml;
  if (pos.open) {
    positionHtml = `<div class="hedge-position">
      <div class="hedge-position-head">
        <strong>Position open</strong>
        <span>${pos.lots} lot${pos.lots === 1 ? "" : "s"} · avg ${fmtInr(pos.average_entry)} · SL ${fmtInr(pos.sl_price)} · T1 ${fmtInr(pos.target_1)} · T2 ${fmtInr(pos.target_2)}</span>
      </div>
      <div class="hedge-note">
        CE ${fmtPrice(pos.ce_strike)} / PE ${fmtPrice(pos.pe_strike)} · ${pos.radius_pts || 0}-pt radius ·
        levels: ${escapeHtml((pos.entry_levels || []).join(", ") || "—")} ·
        booked ${pos.booked_lots || 0} · remaining ${pos.remaining_lots || 0}
        ${data.next_action ? ` · next: ${escapeHtml(data.next_action)}${data.next_level ? ` (${escapeHtml(data.next_level)})` : ""}` : ""}
      </div>
    </div>`;
  } else {
    positionHtml = `<div class="hedge-position">
      <div class="hedge-position-head">
        <strong>No position</strong>
        <span>next action: ${escapeHtml(data.next_action || "WAIT")}${data.next_level ? ` (${escapeHtml(data.next_level)})` : ""}</span>
      </div>
      <div class="hedge-note">
        ${
          data.note
            ? escapeHtml(data.note)
            : `Stop loss ${fmtInr(slPrice)} · T1 ${fmtInr(target1)} · T2 ${fmtInr(target2)} · entry window ${escapeHtml(data.entry_window || "13:00-15:30 IST")}.`
        }
      </div>
    </div>`;
  }

  hedge23Body.innerHTML = `<div class="hedge-grid">${stats}</div>${legsHtml}${positionHtml}
    <div class="hedge-footer">
      <span>Capital ${fmtInr(data.capital)} · cap ${fmtInr(data.max_capital)} · ${escapeHtml(
        data.lot_size ?? "—"
      )}-share lot</span>
      <span>${escapeHtml(data.advisory || "Advisory only.")}</span>
    </div>`;
}

async function loadHedge23(force = false) {
  if (!hedge23Body || pinGateVisible()) return;
  try {
    const data = await getJson(`/api/hedge23/state${force ? "?refresh=true" : ""}`);
    renderHedge23(data);
  } catch (err) {
    if (err.pinRequired) return;
    hedge23Status.textContent = "unavailable";
    hedge23Status.className = "hedge-status-pill danger";
    hedge23Updated.textContent = "";
    hedge23Body.innerHTML = `<div class="placeholder">Hedge 23 snapshot unavailable — ${escapeHtml(err.message)}</div>`;
  }
}

// -- admin console ----------------------------------------------------------
function renderAdmin() {
  adminUpdated.textContent = `updated ${new Date().toLocaleTimeString("en-GB")}`;

  const usage = adminState.usage;
  if (usage) {
    const day = usage.per_day;
    const pct = day.pct_day_fyers ?? 0;
    const used = day.used;
    const limit = day.limit_fyers;
    const remaining = day.remaining_fyers;

    const gauge = document.getElementById("gauge-progress");
    const gaugeValue = document.getElementById("api-used");
    const apiPct = document.getElementById("api-pct");
    const apiLimit = document.getElementById("api-day-limit");
    const apiRemaining = document.getElementById("api-remaining");
    const apiPerSec = document.getElementById("api-per-sec");
    const apiPerMin = document.getElementById("api-per-min");

    if (gauge) {
      const circumference = 2 * Math.PI * 40;
      gauge.style.strokeDasharray = circumference;
      gauge.style.strokeDashoffset = circumference * (1 - pct / 100);
      gauge.style.stroke =
        pct >= 90 ? varDanger() : pct >= 70 ? varAmber() : varAccent();
    }
    if (gaugeValue) {
      gaugeValue.textContent = used;
      gaugeValue.className = "gauge-value" +
        (pct >= 90 ? " danger" : pct >= 70 ? " warn" : "");
    }
    if (apiPct) apiPct.textContent = `${pct}%`;
    if (apiLimit) apiLimit.textContent = limit;
    if (apiRemaining) apiRemaining.textContent = remaining;
    if (apiPerSec) {
      const ps = usage.per_second;
      apiPerSec.textContent = `${ps.used}/${ps.limit_fyers}`;
    }
    if (apiPerMin) {
      const pm = usage.per_minute;
      apiPerMin.textContent = `${pm.used}/${pm.limit_fyers}`;
    }
  }

  const storage = adminState.storage;
  if (storage) {
    const set = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val == null ? "--" : val;
    };
    set("db-path", storage.database_path);
    set("db-size", storage.size_mb != null ? `${storage.size_mb} MB` : "--");
    set("db-signals", storage.total_signals);
    set("db-strategies", storage.strategies);
    set("db-retention", storage.retention_days);
    set("db-oldest", storage.oldest_run ? fmtTime(storage.oldest_run) : "--");
    set("db-latest", storage.latest_run ? fmtTime(storage.latest_run) : "--");
  }
}

function varAccent() { return "#35e0a1"; }
function varAmber() { return "#f5b445"; }
function varDanger() { return "#f0553f"; }

async function loadAdmin() {
  if (!adminPanel) return;
  if (pinGateVisible()) return;
  try {
    const [usage, storage] = await Promise.allSettled([
      getJson("/api/admin/usage"),
      getJson("/api/admin/storage"),
    ]);
    if (usage.status === "fulfilled") adminState.usage = usage.value;
    if (storage.status === "fulfilled") adminState.storage = storage.value;
    renderAdmin();
  } catch {
    /* admin data is non-critical; fail silently */
  }
}

// -- live run feedback ------------------------------------------------------
const RUN_POLL_MS = 1000;
const RUN_HOLD_MS = 12000;

const STAGE_LABELS = {
  prefilter: "Prefiltering universe",
  fetching: "Fetching candles",
  scanning: "Scanning patterns",
};

// Wall-Street mascots: a charging bull (facing right) and a roaring bear
// (facing left), inline SVG so the run pop-up animates without external assets.
const MASCOT_BULL = `<svg class="mascot bull" viewBox="0 0 120 110" aria-hidden="true">
  <defs>
    <linearGradient id="mBullBody" x1="0" y1="0" x2="0.25" y2="1">
      <stop offset="0" stop-color="#4ceaa6"/>
      <stop offset="0.55" stop-color="#27a271"/>
      <stop offset="1" stop-color="#146449"/>
    </linearGradient>
    <linearGradient id="mBullHorn" x1="0" y1="1" x2="0.7" y2="0">
      <stop offset="0" stop-color="#c9a86a"/>
      <stop offset="0.5" stop-color="#f3e9cf"/>
      <stop offset="1" stop-color="#fdf8ea"/>
    </linearGradient>
  </defs>
  <ellipse cx="62" cy="105" rx="46" ry="5" fill="#04070a" opacity="0.28"/>
  <path d="M18 64 C7 60 2 47 11 39 C6 48 11 57 21 60 Z" fill="#146449"/>
  <circle cx="10" cy="38" r="3.2" fill="#146449"/>
  <path d="M30 84 C25 93 23 99 21 105 L34 105 C36 98 38 92 41 86 Z" fill="#0f4d37"/>
  <path d="M84 82 C82 91 80 99 80 105 L93 105 C95 97 95 89 93 82 Z" fill="#0f4d37"/>
  <path d="M16 96 C10 74 20 52 44 42 C62 34 84 34 98 48 C112 62 108 84 92 92 C72 102 34 104 16 96 Z" fill="url(#mBullBody)" stroke="#0d3f2c" stroke-width="1.5"/>
  <path d="M60 38 C78 32 94 37 101 49 C90 40 76 38 60 38 Z" fill="#63e7ab" opacity="0.5"/>
  <path d="M50 52 C64 47 80 48 90 55" fill="none" stroke="#0d3f2c" stroke-width="1.4" opacity="0.3"/>
  <path d="M42 88 C40 96 40 101 38 105 L53 105 C55 98 56 92 56 88 Z" fill="url(#mBullBody)" stroke="#0d3f2c" stroke-width="1.4"/>
  <path d="M90 84 C94 76 102 72 110 74 C104 78 100 82 98 88 C95 90 92 88 90 84 Z" fill="url(#mBullBody)" stroke="#0d3f2c" stroke-width="1.4"/>
  <path d="M94 48 C100 38 113 35 119 42 C125 49 122 61 115 66 C108 71 99 68 95 61 C92 57 92 52 94 48 Z" fill="url(#mBullBody)" stroke="#0d3f2c" stroke-width="1.5"/>
  <path d="M110 58 C116 54 121 56 121 61 C121 66 116 68 110 66 C107 63 107 60 110 58 Z" fill="#8ff0c0"/>
  <circle cx="117.5" cy="61.5" r="1.5" fill="#0b2f22"/>
  <path d="M108 66 C112 68 116 68 119 66" fill="none" stroke="#0b2f22" stroke-width="1.1" stroke-linecap="round"/>
  <circle cx="106" cy="50" r="2.7" fill="#0b2f22"/>
  <circle cx="106.8" cy="49.1" r="0.9" fill="#ffffff"/>
  <path d="M100 45 C103 43 108 43 111 45" fill="none" stroke="#0b2f22" stroke-width="1.5" stroke-linecap="round"/>
  <ellipse cx="101" cy="48" rx="4.4" ry="2.6" fill="#146449" transform="rotate(-18 101 48)"/>
  <path d="M93 46 C88 34 92 23 103 17 C96 26 95 36 99 46 Z" fill="#b09457"/>
  <path d="M102 44 C100 29 107 17 119 12 C111 21 108 31 110 43 Z" fill="url(#mBullHorn)"/>
</svg>`;

const MASCOT_BEAR = `<svg class="mascot bear" viewBox="0 0 120 110" aria-hidden="true">
  <defs>
    <linearGradient id="mBearBody" x1="0" y1="0" x2="0.3" y2="1">
      <stop offset="0" stop-color="#a2593f"/>
      <stop offset="0.55" stop-color="#743a29"/>
      <stop offset="1" stop-color="#3f1d14"/>
    </linearGradient>
    <linearGradient id="mBearSnout" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#c98a6b"/>
      <stop offset="1" stop-color="#9a5b41"/>
    </linearGradient>
  </defs>
  <ellipse cx="58" cy="105" rx="46" ry="5" fill="#04070a" opacity="0.28"/>
  <circle cx="104" cy="66" r="4.5" fill="#3f1d14"/>
  <path d="M88 84 C92 92 94 99 96 105 L84 105 C82 98 80 92 78 86 Z" fill="#2e120c"/>
  <path d="M36 82 C38 91 40 99 40 105 L28 105 C26 97 26 89 28 82 Z" fill="#2e120c"/>
  <path d="M104 96 C110 74 100 52 76 42 C58 34 36 34 22 48 C8 62 12 84 28 92 C48 102 86 104 104 96 Z" fill="url(#mBearBody)" stroke="#2e120c" stroke-width="1.5"/>
  <path d="M52 40 C64 30 82 30 90 41 C79 34 64 34 52 40 Z" fill="#c98a6b" opacity="0.35"/>
  <path d="M30 60 C28 70 30 80 36 87" fill="none" stroke="#2e120c" stroke-width="1.4" opacity="0.3"/>
  <path d="M92 52 C98 62 100 74 96 84" fill="none" stroke="#2e120c" stroke-width="1.4" opacity="0.3"/>
  <path d="M74 88 C76 96 77 101 79 105 L64 105 C62 98 60 92 60 88 Z" fill="url(#mBearBody)" stroke="#2e120c" stroke-width="1.4"/>
  <path d="M28 80 C22 71 11 67 4 71 C10 75 14 79 16 85 C20 88 26 86 28 80 Z" fill="url(#mBearBody)" stroke="#2e120c" stroke-width="1.4"/>
  <path d="M6 70 L3 66 M9 71 L7 67 M12 73 L11 69" stroke="#f4ead8" stroke-width="1.6" stroke-linecap="round"/>
  <circle cx="63" cy="22" r="4.2" fill="#2e120c"/>
  <path d="M38 50 C32 40 36 29 48 25 C60 21 72 27 74 38 C76 49 70 57 58 59 C50 61 42 57 38 50 Z" fill="url(#mBearBody)" stroke="#2e120c" stroke-width="1.5"/>
  <circle cx="46" cy="24" r="4.8" fill="url(#mBearBody)" stroke="#2e120c" stroke-width="1.3"/>
  <circle cx="46" cy="24" r="2.2" fill="#5c2a1e"/>
  <path d="M30 54 C24 51 18 53 17 58 C16 63 22 66 29 65 C34 64 35 57 30 54 Z" fill="url(#mBearSnout)"/>
  <ellipse cx="20" cy="56" rx="3.2" ry="2.5" fill="#24100b"/>
  <path d="M22 64 C28 68 38 68 44 63 C43 71 33 75 25 71 Z" fill="#33110f"/>
  <path d="M28 66 l2.3 3.4 l2.3 -3.4 z" fill="#f4ead8"/>
  <path d="M35 66 l2.3 3.4 l2.3 -3.4 z" fill="#f4ead8"/>
  <ellipse cx="36" cy="69" rx="4" ry="2.2" fill="#c25b45"/>
  <circle cx="52" cy="36" r="2.5" fill="#1a0c08"/>
  <circle cx="52.8" cy="35.1" r="0.85" fill="#ffffff"/>
  <path d="M46 32 C49 30 54 30 57 32" fill="none" stroke="#1a0c08" stroke-width="1.5" stroke-linecap="round"/>
</svg>`;

const SPARK = `<svg class="spark" viewBox="0 0 40 40" aria-hidden="true">
  <defs>
    <linearGradient id="mSpark" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#fff8d6"/>
      <stop offset="0.5" stop-color="#ffd76a"/>
      <stop offset="1" stop-color="#f5a524"/>
    </linearGradient>
  </defs>
  <path d="M20 1 L24.5 15.5 L39 20 L24.5 24.5 L20 39 L15.5 24.5 L1 20 L15.5 15.5 Z" fill="url(#mSpark)" opacity="0.95"/>
</svg>`;

let modalEl = null;
let modalRenderKey = "";
const closeTimers = new Map();

function fmtElapsed(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function runElapsed(run) {
  return run.status.elapsed_s != null ? run.status.elapsed_s : (Date.now() - run.startedAt) / 1000;
}

function runProgress(run) {
  const st = run.status;
  return st.stage === "fetching" && st.total ? Math.round((st.done / st.total) * 100) : null;
}

function runRenderKey(run) {
  const st = run.status;
  return `${st.state}|${st.stage}`;
}

function fighterHtml(side) {
  const mascot = side === "bull" ? MASCOT_BULL : MASCOT_BEAR;
  return `<span class="fighter ${side}"><span class="dust"></span>${mascot}</span>`;
}

function modalHtml(name, run) {
  const st = run.status;
  const n = st.signals ?? 0;
  const fighters = `${fighterHtml("bull")}<span class="clash">${SPARK}</span>${fighterHtml("bear")}`;
  let tone = "running";
  let stageRow;
  let extras = "";
  if (st.state === "done") {
    tone = n > 0 ? "done" : "flat";
    stageRow =
      n > 0
        ? `<span class="verdict">${n} signal${n === 1 ? "" : "s"} found</span><span class="count">finished in ${fmtElapsed(st.elapsed_s)}</span>`
        : `<span class="verdict muted">No matches this run</span><span class="count">finished in ${fmtElapsed(st.elapsed_s)}</span>`;
  } else if (st.state === "error") {
    tone = "error";
    stageRow = `<span class="verdict err">${escapeHtml(st.error || "Run failed")}</span><span class="count">failed after ${fmtElapsed(st.elapsed_s)}</span>`;
  } else {
    const label = STAGE_LABELS[st.stage] || "Starting";
    const counter = st.stage === "fetching" && st.total ? `${st.done} / ${st.total}` : "";
    stageRow = `<span data-role="modal-stage">${label}…</span><span class="count" data-role="modal-count">${counter}</span>`;
    const pct = runProgress(run);
    const bar =
      pct == null
        ? '<div class="run-bar indet"><i></i></div>'
        : `<div class="run-bar"><i data-role="modal-fill" style="width:${pct}%"></i></div>`;
    extras = `${bar}<div class="run-hint">the scan keeps running on the server — you can leave this page</div>`;
  }
  const close =
    st.state === "running"
      ? `<div class="run-modal-actions">
          <button class="btn ghost run-modal-minimize" type="button" title="Minimize — scan keeps running">−</button>
          <button class="btn run-modal-close" type="button">Close</button>
        </div>`
      : `<div class="run-modal-actions"><button class="btn ghost run-modal-close" type="button">Close</button></div>`;
  return `<div class="run-modal ${tone}" role="dialog" aria-modal="true" aria-label="${escapeHtml(name)} run">
    <div class="run-modal-head">
      <div class="run-modal-title">${escapeHtml(name)}</div>
      <div class="run-modal-time" data-role="modal-time">${fmtElapsed(runElapsed(run))}</div>
    </div>
    <div class="arena">${fighters}</div>
    <div class="run-modal-stage">${stageRow}</div>
    ${extras}
    ${close}
  </div>`;
}

function openRunModal(name) {
  const run = state.runs.get(name);
  if (!run) return;
  if (!modalEl) {
    modalEl = document.createElement("div");
    modalEl.className = "run-modal-overlay";
    document.body.appendChild(modalEl);
  }
  state.modalName = name;
  modalRenderKey = runRenderKey(run);
  modalEl.innerHTML = modalHtml(name, run);
  wireModalClose(name);

  // Play sound once per run state transition.
  const key = `${name}:${run.status.state}`;
  if (!playedSounds.has(key)) {
    playedSounds.add(key);
    if (run.status.state === "running") {
      playScanStart();
    } else if (run.status.state === "done") {
      const n = run.status.signals ?? 0;
      playScanDone(n > 0);
    }
  }
}

function wireModalClose(name) {
  const btn = modalEl && modalEl.querySelector(".run-modal-close");
  if (btn) btn.addEventListener("click", () => endRunUi(name));
  const minBtn = modalEl && modalEl.querySelector(".run-modal-minimize");
  if (minBtn) minBtn.addEventListener("click", () => minimizeRun(name));
}

// -- minimized runs (background scanning) ------------------------------------
function minimizeRun(name) {
  state.minimizedRuns.add(name);
  dismissRunModal();
  renderMinimizedBar();
}

function restoreRun(name) {
  state.minimizedRuns.delete(name);
  state.modalName = name;
  const run = state.runs.get(name);
  if (!run) return;
  modalRenderKey = runRenderKey(run);
  if (!modalEl) {
    modalEl = document.createElement("div");
    modalEl.className = "run-modal-overlay";
    document.body.appendChild(modalEl);
  }
  modalEl.innerHTML = modalHtml(name, run);
  wireModalClose(name);
  renderMinimizedBar();
}

function renderMinimizedBar() {
  const existing = document.getElementById("minimized-runs-bar");
  if (existing) existing.remove();
  if (!state.minimizedRuns.size) return;
  const bar = document.createElement("div");
  bar.id = "minimized-runs-bar";
  bar.className = "minimized-runs-bar";
  const items = [...state.minimizedRuns]
    .map(
      (n) => {
        const run = state.runs.get(n);
        if (!run) return "";
        const elapsed = fmtElapsed(runElapsed(run));
        return `<button class="minimized-run-item" data-name="${escapeHtml(n)}" title="${escapeHtml(n)} — running">
          <span class="minimized-dot"></span>
          <span class="minimized-name">${escapeHtml(n)}</span>
          <span class="minimized-time">${elapsed}</span>
        </button>`;
      }
    )
    .join("");
  bar.innerHTML = items;
  bar.querySelectorAll(".minimized-run-item").forEach((el) => {
    el.addEventListener("click", () => restoreRun(el.dataset.name));
  });
  document.body.appendChild(bar);
  // auto-hide after 3s
  setTimeout(() => {
    bar.classList.add("fade-out");
    setTimeout(() => bar.remove(), 400);
  }, 3000);
}

function updateMinimizedTimes() {
  const bar = document.getElementById("minimized-runs-bar");
  if (!bar) return;
  [...state.minimizedRuns].forEach((name) => {
    const run = state.runs.get(name);
    if (!run) return;
    const item = bar.querySelector(`[data-name="${escapeHtml(name)}"]`);
    if (item) {
      const time = item.querySelector(".minimized-time");
      if (time) time.textContent = fmtElapsed(runElapsed(run));
    }
  });
}

function removeMinimizedRun(name) {
  state.minimizedRuns.delete(name);
  renderMinimizedBar();
}

function scheduleEndRunUi(name) {
  cancelEndRunUi(name);
  closeTimers.set(
    name,
    setTimeout(() => {
      closeTimers.delete(name);
      endRunUi(name);
    }, RUN_HOLD_MS)
  );
}

function cancelEndRunUi(name) {
  const id = closeTimers.get(name);
  if (id) {
    clearTimeout(id);
    closeTimers.delete(name);
  }
}

function endRunUi(name) {
  cancelEndRunUi(name);
  const run = state.runs.get(name);
  if (run && run.status.state === "running") return;
  state.runs.delete(name);
  state.minimizedRuns.delete(name);
  updateRunUI(name);
  if (state.modalName === name) dismissRunModal();
  renderMinimizedBar();
}

function dismissRunModal() {
  if (!modalEl) return;
  state.modalName = null;
  modalRenderKey = "";
  const el = modalEl;
  modalEl = null;
  el.classList.add("closing");
  setTimeout(() => el.remove(), 220);
}

function updateRunUI(name) {
  const card = [...resultsEl.querySelectorAll(".strategy-card")].find(
    (c) => c.dataset.name === name
  );
  const run = state.runs.get(name);
  if (card) {
    const isRunning = Boolean(run && run.status.state === "running");
    card.classList.toggle("running", isRunning);
    const btn = card.querySelector(".run-btn");
    if (btn) {
      if (isRunning) {
        if (!btn.querySelector(".spinner")) {
          btn.disabled = true;
          btn.innerHTML = '<span class="spinner"></span>Running <span data-role="btn-time"></span>';
        }
        const t = btn.querySelector('[data-role="btn-time"]');
        if (t) t.textContent = fmtElapsed(runElapsed(run));
      } else {
        btn.disabled = false;
        btn.textContent = "Run now";
      }
    }
  }
  if (!run || !modalEl || state.modalName !== name) return;
  if (modalRenderKey !== runRenderKey(run)) {
    const prevState = modalRenderKey.split("|")[0];
    modalRenderKey = runRenderKey(run);
    modalEl.innerHTML = modalHtml(name, run);
    wireModalClose(name);
    // Play done sound when run transitions to done.
    if (prevState === "running" && run.status.state === "done" && !playedSounds.has(`${name}:done`)) {
      playedSounds.add(`${name}:done`);
      const n = run.status.signals ?? 0;
      playScanDone(n > 0);
    }
    return;
  }
  if (run.status.state !== "running") return;
  // Same stage: update the live numbers in place so mascot/bar animations
  // are not restarted on every poll tick.
  const fill = modalEl.querySelector('[data-role="modal-fill"]');
  if (fill) {
    const pct = runProgress(run);
    if (pct != null) fill.style.width = `${pct}%`;
  }
  const stage = modalEl.querySelector('[data-role="modal-stage"]');
  if (stage) stage.textContent = `${STAGE_LABELS[run.status.stage] || "Starting"}…`;
  const count = modalEl.querySelector('[data-role="modal-count"]');
  if (count) {
    count.textContent =
      run.status.stage === "fetching" && run.status.total
        ? `${run.status.done} / ${run.status.total}`
        : "";
  }
  const time = modalEl.querySelector('[data-role="modal-time"]');
  if (time) time.textContent = fmtElapsed(runElapsed(run));
}

async function runStrategy(name) {
  const existing = state.runs.get(name);
  if (existing && existing.status.state === "running") return;

  const run = {
    startedAt: Date.now(),
    status: { state: "running", stage: "starting", done: 0, total: 0 },
  };
  state.runs.set(name, run);
  renderResults();
  openRunModal(name);

  const poll = setInterval(async () => {
    try {
      const st = await getJson(`/api/run/${encodeURIComponent(name)}/status`);
      if (st.state !== "idle" && state.runs.get(name) === run) {
        run.status = st;
        updateRunUI(name);
      }
    } catch {
      /* transient poll failure - the next tick retries */
    }
  }, RUN_POLL_MS);

  try {
    const data = await getJson(`/api/run/${encodeURIComponent(name)}`, { method: "POST" });
    const final = await getJson(`/api/run/${encodeURIComponent(name)}/status`).catch(() => null);
    run.status =
      final && final.state !== "idle"
        ? final
        : { state: "done", signals: data.signals, elapsed_s: (Date.now() - run.startedAt) / 1000 };
    const n = data.signals;
    toast(`✓ ${name}: ${n} signal${n === 1 ? "" : "s"} in ${fmtElapsed(run.status.elapsed_s)}`);
  } catch (err) {
    run.status = {
      state: "error",
      error: err.message,
      elapsed_s: (Date.now() - run.startedAt) / 1000,
    };
    toast(`${name}: ${err.message}`, true);
  } finally {
    clearInterval(poll);
    updateRunUI(name);
    scheduleEndRunUi(name);
  }
  loadResults().catch(() => {});
}

async function toggleHistory(name, btn, card) {
  const block = card.querySelector(".history-block");
  if (!block.classList.contains("hidden")) {
    block.classList.add("hidden");
    btn.textContent = "History";
    return;
  }
  btn.disabled = true;
  btn.textContent = "Loading…";
  try {
    const data = await getJson(`/api/history/${encodeURIComponent(name)}?days=14`);
    const banded = data.signals.some((s) => (s.metadata || {}).band);
    const rows = banded
      ? data.signals.filter((s) => (s.metadata || {}).band === "BUY")
      : data.signals;
    block.innerHTML = `<h4>Signals · last 14 days</h4>${historyTable(rows)}`;
    block.classList.remove("hidden");
    btn.textContent = "Hide history";
  } catch (err) {
    toast(`${name}: ${err.message}`, true);
    btn.textContent = "History";
  } finally {
    btn.disabled = false;
  }
}

// -- wiring -----------------------------------------------------------------
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !state.modalName) return;
  const run = state.runs.get(state.modalName);
  if (!run || run.status.state !== "running") endRunUi(state.modalName);
});

filtersEl.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  filtersEl.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
  chip.classList.add("active");
  state.filter = chip.dataset.cat;
  renderResults();
});

resultsEl.addEventListener("click", (event) => {
  const runBtn = event.target.closest(".run-btn");
  if (runBtn) {
    runStrategy(runBtn.dataset.name);
    return;
  }
  const expandBtn = event.target.closest(".expand-btn");
  if (expandBtn) {
    const name = expandBtn.dataset.name;
    if (state.expanded.has(name)) {
      state.expanded.delete(name);
    } else {
      state.expanded.add(name);
    }
    renderResults();
    return;
  }
  const subtabBtn = event.target.closest(".subtab-chip");
  if (subtabBtn) {
    const name = subtabBtn.dataset.name;
    const subtab = subtabBtn.dataset.subtab;
    state.subtabs.set(name, subtab);
    renderResults();
    return;
  }
  const histBtn = event.target.closest(".history-btn");
  if (histBtn) {
    toggleHistory(histBtn.dataset.name, histBtn, histBtn.closest(".strategy-card"));
  }
});

document.getElementById("refresh-overview").addEventListener("click", () => loadOverview(true));

if (refreshHedge23Btn) {
  refreshHedge23Btn.addEventListener("click", () => loadHedge23(true));
}

document.getElementById("refresh-all").addEventListener("click", async () => {
  await Promise.allSettled([
    checkHealth(),
    loadOverview(true),
    loadHedge23(true),
    loadResults(),
    loadAdmin(),
  ]);
});

adminToggle.addEventListener("click", () => {
  const isHidden = adminPanel.classList.contains("hidden");
  if (isHidden) {
    adminPanel.classList.remove("hidden");
    adminPanel.style.display = "block";
    loadAdmin();
  } else {
    adminPanel.classList.add("hidden");
    adminPanel.style.display = "none";
  }
});

refreshAdminBtn.addEventListener("click", () => loadAdmin());

overviewToggle.addEventListener("click", () => {
  const isHidden = overviewPanel.classList.contains("hidden");
  if (isHidden) {
    overviewPanel.classList.remove("hidden");
    overviewPanel.classList.add("visible");
    overviewPanel.style.display = "block";
    loadOverview();
  } else {
    overviewPanel.classList.add("hidden");
    overviewPanel.classList.remove("visible");
    overviewPanel.style.display = "none";
  }
});

async function init() {
  await checkHealth();
  if (await authNeeded()) {
    showPinGate();
    return;
  }
  hidePinGate();
  await loadOverview();
  await loadHedge23();
  await loadAdmin();
  try {
    await loadResults();
  } catch (err) {
    if (!err.pinRequired) {
      resultsEl.innerHTML = `<div class="placeholder">Could not load strategies — ${escapeHtml(err.message)}</div>`;
    }
  }
}

init();
setInterval(checkHealth, REFRESH_MS);
setInterval(loadHedge23, REFRESH_MS);
setInterval(loadResults, REFRESH_MS);
setInterval(loadAdmin, REFRESH_MS);
setInterval(updateMinimizedTimes, 1000);
