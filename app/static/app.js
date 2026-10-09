"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n, d = 2) => (n == null || isNaN(n)) ? "–" : Number(n).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const rupee = (n) => (n == null ? "–" : "₹" + inr(n));
const pnlCls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");
const signed = (n) => `<span class="${pnlCls(n)}">${n > 0 ? "+" : ""}${inr(n)}</span>`;
const tm = (iso) => (iso ? String(iso).slice(11, 16) : "");
const dt = (iso) => String(iso || "").slice(0, 16).replace("T", " ");

const state = { snap: null, view: "watch", openRow: null, chartSym: null, order: null, settings: null,
  seg: { orders: "open", portfolio: "positions" }, orders: [] };

async function api(method, url, body) {
  const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : res.statusText);
  return data;
}
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 3000);
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); return r; } catch (e) { toast(e.message, true); throw e; }
}
const empty = (msg) => `<div class="empty">${msg}</div>`;

/* ---------------- sheets ---------------- */
function openSheet(id) { $(id).classList.remove("hidden"); document.body.style.overflow = "hidden"; }
function closeSheet(id) {
  $(id).classList.add("hidden");
  if (!$$(".sheet").some((s) => !s.classList.contains("hidden"))) document.body.style.overflow = "";
  if (id === "#orderSheet") state.order = null;
  if (id === "#chartSheet") state.chartSym = null;
}
$$(".sheet").forEach((s) => s.addEventListener("click", (e) => {
  if (e.target === s || e.target.closest("[data-close]")) closeSheet("#" + s.id);
}));

/* ---------------- charts ---------------- */
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function mkChart(el) {
  return LightweightCharts.createChart(el, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: css("--muted"), fontSize: 11 },
    grid: { vertLines: { visible: false }, horzLines: { color: css("--border") } },
    timeScale: { timeVisible: true, secondsVisible: false, borderColor: css("--border") },
    rightPriceScale: { borderColor: css("--border") },
    handleScale: { axisPressedMouseMove: false },
  });
}
const charts = {};
function initCharts() {
  const c = (charts.price = mkChart($("#chart")));
  charts.candles = c.addCandlestickSeries({ upColor: "#15a05a", downColor: "#e0383e", borderVisible: false, wickUpColor: "#15a05a", wickDownColor: "#e0383e" });
  charts.ema20 = c.addLineSeries({ color: "#f59e0b", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  charts.ema50 = c.addLineSeries({ color: "#8b5cf6", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  charts.pnl = mkChart($("#pnlChart"));
  charts.pnlLine = charts.pnl.addBaselineSeries({ baseValue: { type: "price", price: 0 }, lineWidth: 2 });
  charts.bt = mkChart($("#btChart"));
  charts.btLine = charts.bt.addAreaSeries({ lineColor: "#4184f3", topColor: "rgba(65,132,243,.25)", bottomColor: "rgba(65,132,243,0)", lineWidth: 2 });
}
const isoToChart = (iso) => Math.floor(new Date(iso).getTime() / 1000) + 330 * 60; // IST wall clock

async function openChart(sym) {
  state.chartSym = sym;
  $("#chartTitle").textContent = sym;
  openSheet("#chartSheet");
  await loadChart();
}
async function loadChart() {
  const sym = state.chartSym; if (!sym || !charts.candles) return;
  const w = state.snap?.watchlist.find((x) => x.symbol === sym);
  $("#chartLtp").textContent = w?.ltp ? "₹" + inr(w.ltp) : "";
  $("#chartSignal").innerHTML = w?.action ? `<span class="sigchip ${w.action}">${w.action} ${inr(w.score, 2)}</span> ${esc(w.reason)}` : "";
  try {
    const d = await api("GET", `/api/candles/${encodeURIComponent(sym)}`);
    charts.candles.setData(d.candles); charts.ema20.setData(d.ema20); charts.ema50.setData(d.ema50);
    const times = d.candles.map((c) => c.time);
    const trades = await api("GET", "/api/trades?limit=200");
    const marks = trades.filter((t) => t.symbol === sym && times.length && isoToChart(t.time) >= times[0]).map((t) => {
      const ts = isoToChart(t.time); let at = times[0];
      for (const x of times) { if (x <= ts) at = x; else break; }
      return { time: at, position: t.side === "BUY" ? "belowBar" : "aboveBar", color: t.side === "BUY" ? "#4184f3" : "#df514c",
        shape: t.side === "BUY" ? "arrowUp" : "arrowDown", text: t.side[0] + t.qty };
    }).sort((a, b) => a.time - b.time);
    charts.candles.setMarkers(marks);
  } catch (e) { $("#chartSignal").textContent = "Chart unavailable: " + e.message; }
}
$("#chartBuy").addEventListener("click", () => openOrder(state.chartSym, "BUY"));
$("#chartSell").addEventListener("click", () => openOrder(state.chartSym, "SELL"));

async function loadPnl() {
  if (!charts.pnlLine) return;
  const rows = await api("GET", "/api/equity");
  const today = state.snap?.time?.slice(0, 10);
  const pts = []; let last = 0;
  for (const r of rows) {
    if (today && !r.time.startsWith(today)) continue;
    const t = isoToChart(r.time); if (t > last) { pts.push({ time: t, value: r.day_pnl }); last = t; }
  }
  charts.pnlLine.setData(pts);
}

/* ---------------- header & snapshot ---------------- */
function render(s) {
  state.snap = s;
  const badge = $("#modeBadge");
  badge.textContent = s.mode === "live" ? "LIVE" : "PAPER"; badge.className = "badge" + (s.mode === "live" ? " live" : "");
  $("#kEquity").textContent = rupee(s.funds.equity);
  $("#kPnl").innerHTML = signed(s.day_pnl);
  $("#kAvail").textContent = rupee(s.funds.available);
  const kill = $("#btnKill");
  kill.textContent = s.kill_switch ? "RESET" : "KILL"; kill.classList.toggle("reset", s.kill_switch);

  const notes = [];
  if (!s.password_set && !["localhost", "127.0.0.1"].includes(location.hostname)) notes.push("⚠ No APP_PASSWORD set: anyone with this link can trade. Set it in your host's environment variables.");
  if (!s.connected) notes.push("Not connected to Kite — log in from Account.");
  if (s.kill_switch) notes.push("Kill switch ON: trading blocked until you tap RESET.");
  if (s.halted) notes.push("Bot halted for today: " + s.halted);
  if (s.data_source === "simulated") notes.push("Demo prices (simulated). Use DATA_SOURCE=yahoo or kite for real data.");
  else if (!s.market_open) notes.push("Market closed. The bot trades 09:15–15:30 IST, Mon–Fri.");
  const b = $("#banner"); b.innerHTML = notes.map(esc).join("<br>"); b.classList.toggle("hidden", !notes.length);

  // bot card
  const bs = $("#botState");
  bs.textContent = s.kill_switch ? "Killed" : s.halted ? "Halted for today" : s.running ? "Bot running" : "Bot off";
  bs.className = "bot-state" + (s.running && !s.halted ? " on" : s.halted || s.kill_switch ? " halt" : "");
  $("#botSub").textContent = s.running ? `Watching ${s.watchlist.length} stocks · ${s.positions.filter((p) => p.protected && p.source === "bot" && p.qty).length} bot positions` : "Auto-trading is paused. Open positions stay protected.";
  const bb = $("#btnBot"); bb.textContent = s.running ? "Stop" : "Start"; bb.className = "btn big " + (s.running ? "sell" : "buy");

  renderWatch(s);
  if (state.view === "portfolio" && state.seg.portfolio === "positions") renderPositions(s.positions);
  if (state.view === "bot") { renderSignals(s.watchlist); renderEvents(s.events); }
  if (state.order) updateOrderInfo();
  if (state.chartSym) { const w = s.watchlist.find((x) => x.symbol === state.chartSym); if (w?.ltp) $("#chartLtp").textContent = "₹" + inr(w.ltp); }
}

/* ---------------- watchlist ---------------- */
function renderWatch(s) {
  const el = $("#watchList");
  if (!s.watchlist.length) { el.innerHTML = empty("Your watchlist is empty. Add a stock above."); return; }
  el.innerHTML = s.watchlist.map((w) => {
    const pos = s.positions.find((p) => p.symbol === w.symbol && p.qty);
    return `<li data-sym="${esc(w.symbol)}" class="${state.openRow === w.symbol ? "open" : ""}">
      <div class="li-row"><span class="sym">${esc(w.symbol)}</span><span class="num">${inr(w.ltp)}</span></div>
      <div class="li-row li-sub"><span>${w.action ? `<span class="sigchip ${w.action}">${w.action}</span> ${inr(w.score, 2)}` : "…"}
        ${pos ? `<span class="tag">${pos.qty > 0 ? "+" : ""}${pos.qty} held</span>` : ""}</span><span>${esc(w.exchange)}</span></div>
      <div class="w-actions">
        <button class="btn buy" data-a="BUY">Buy</button>
        <button class="btn sell" data-a="SELL">Sell</button>
        <button class="btn" data-a="chart">Chart</button>
        <button class="btn" data-a="rm" aria-label="Remove">✕</button>
      </div></li>`;
  }).join("");
}
$("#watchList").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-sym]"); if (!li) return;
  const sym = li.dataset.sym, a = e.target.closest("[data-a]")?.dataset.a;
  if (a === "BUY" || a === "SELL") return openOrder(sym, a);
  if (a === "chart") return openChart(sym);
  if (a === "rm") {
    if (!confirm(`Remove ${sym} from the watchlist? The bot will stop trading it.`)) return;
    const wl = state.snap.watchlist.map((w) => w.symbol).filter((x) => x !== sym);
    await act(() => api("PATCH", "/api/settings", { watchlist: wl }), `${sym} removed`);
    return refresh();
  }
  state.openRow = state.openRow === sym ? null : sym;
  renderWatch(state.snap);
});
$("#addSymbol").addEventListener("submit", async (e) => {
  e.preventDefault();
  const sym = $("#symInput").value.trim().toUpperCase(); if (!sym) return;
  const wl = [...new Set([...state.snap.watchlist.map((w) => w.symbol), sym])];
  await act(() => api("PATCH", "/api/settings", { watchlist: wl }), `${sym} added`);
  $("#symInput").value = ""; $("#symInput").blur(); refresh();
});

/* ---------------- portfolio ---------------- */
const pkey = (p) => `${p.symbol}|${p.exchange}|${p.product}`;
function renderPositions(ps) {
  const total = ps.reduce((a, p) => a + (p.pnl || 0), 0);
  $("#pfSummary").innerHTML = `<span class="muted">Total P&amp;L</span><b>${signed(total)}</b>`;
  const el = $("#portfolioBody");
  if (!ps.length) { el.innerHTML = empty("No positions today"); return; }
  const open = ps.filter((p) => p.qty), closed = ps.filter((p) => !p.qty);
  el.innerHTML = `<ul class="list">${[...open, ...closed].map((p) => `
    <li data-k="${esc(pkey(p))}">
      <div class="li-row"><span><span class="tag">${esc(p.product)}</span> <span class="sym">${esc(p.symbol)}</span></span><b class="num">${signed(p.pnl)}</b></div>
      <div class="li-row li-sub"><span>${p.qty ? `Qty <b class="${p.qty > 0 ? "side-BUY" : "side-SELL"}">${p.qty}</b> · Avg ${inr(p.avg_price)}` : "Closed"}</span><span>LTP ${inr(p.ltp)}</span></div>
      ${p.protected ? `<div class="li-row li-sub"><span>🛡 ${esc(p.source)} · stop ${inr(p.stop_price)}</span><span>target ${inr(p.target_price)}</span></div>` : ""}
      ${p.qty ? `<div class="pos-actions">
        <button class="btn sm" data-pa="add">Add</button>
        <button class="btn sm" data-pa="protect">${p.protected ? "Unprotect" : "🛡 Protect"}</button>
        <button class="btn sm sell" data-pa="exit">Exit</button></div>` : ""}
    </li>`).join("")}</ul>`;
}
$("#portfolioBody").addEventListener("click", async (e) => {
  const li = e.target.closest("li[data-k]"); const a = e.target.closest("[data-pa]")?.dataset.pa;
  if (!li || !a) return;
  const [symbol, exchange, product] = li.dataset.k.split("|");
  const p = state.snap.positions.find((x) => pkey(x) === li.dataset.k);
  if (a === "exit") {
    if (!confirm(`Exit ${symbol} (${product}) at market?`)) return;
    await act(() => api("POST", "/api/positions/exit", { symbol, exchange, product }), `${symbol} exit sent`);
  } else if (a === "protect") {
    await act(() => api("POST", "/api/positions/protect", { symbol, exchange, product, enabled: !p.protected }),
      p.protected ? "Protection removed" : `${symbol} is now protected`);
  } else if (a === "add") {
    return openOrder(symbol, p.qty > 0 ? "BUY" : "SELL", { exchange, product });
  }
  refresh();
});

async function loadHoldings() {
  const h = await api("GET", "/api/holdings");
  const inv = h.reduce((a, r) => a + r.avg_price * r.qty, 0), pnl = h.reduce((a, r) => a + r.pnl, 0);
  $("#pfSummary").innerHTML = `<span class="muted">Invested ${rupee(inv)}</span><b>${signed(pnl)}</b>`;
  $("#portfolioBody").innerHTML = h.length ? `<ul class="list">${h.map((r) => `
    <li data-sym="${esc(r.symbol)}" data-ex="${esc(r.exchange)}">
      <div class="li-row"><span class="sym">${esc(r.symbol)}</span><b class="num">${signed(r.pnl)}</b></div>
      <div class="li-row li-sub"><span>Qty ${r.qty} · Avg ${inr(r.avg_price)}</span><span>LTP ${inr(r.ltp)}</span></div>
      <div class="pos-actions"><span></span><span></span><button class="btn sm sell" data-sellh>Sell</button></div>
    </li>`).join("")}</ul>` : empty("No holdings. Buy with Longterm (CNC) to hold overnight.");
}
$("#portfolioBody").addEventListener("click", (e) => {
  if (!e.target.closest("[data-sellh]")) return;
  const li = e.target.closest("li"); openOrder(li.dataset.sym, "SELL", { exchange: li.dataset.ex, product: "CNC" });
});

/* ---------------- orders ---------------- */
const isOpen = (o) => o.status === "OPEN" || o.status === "TRIGGER PENDING";
function orderLi(o) {
  const px = o.average_price ? inr(o.average_price) : o.price ? inr(o.price) : "MKT";
  return `<li data-oid="${esc(o.order_id)}">
    <div class="li-row"><span><span class="side-${o.side}">${o.side}</span> <span class="sym">${esc(o.symbol)}</span></span><span class="status ${esc(o.status.split(" ")[0])}">${esc(o.status)}</span></div>
    <div class="li-row li-sub"><span>${o.filled_qty}/${o.qty} · ${esc(o.order_type)} · ${esc(o.product)}${o.trigger_price ? " · trg " + inr(o.trigger_price) : ""}</span><span class="num">${px}</span></div>
    <div class="li-row li-sub"><span>${tm(o.time)} · ${esc(o.tag)}</span><span>${esc(o.status_message)}</span></div></li>`;
}
async function loadOrders() {
  const seg = state.seg.orders, el = $("#ordersBody");
  if (seg === "trades") {
    const trades = await api("GET", "/api/trades?limit=300");
    el.innerHTML = trades.length ? `<ul class="list">${trades.map((t) => `<li>
      <div class="li-row"><span><span class="side-${t.side}">${t.side}</span> <span class="sym">${esc(t.symbol)}</span></span><span class="num">${t.qty} @ ${inr(t.price)}</span></div>
      <div class="li-row li-sub"><span>${esc(dt(t.time))} · ${esc(t.product)} · ${esc(t.tag)}</span><span>${t.realized ? signed(t.realized) : ""} <span class="muted">fee ${inr(t.charges)}</span></span></div></li>`).join("")}</ul>` : empty("No trades yet");
    return;
  }
  state.orders = await api("GET", "/api/orders");
  const list = state.orders.filter((o) => (seg === "open" ? isOpen(o) : !isOpen(o)));
  el.innerHTML = list.length ? `<ul class="list">${list.map(orderLi).join("")}</ul>` : empty(seg === "open" ? "No open orders" : "No executed orders today");
}
$("#ordersBody").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-oid]"); if (!li) return;
  const o = state.orders.find((x) => x.order_id === li.dataset.oid);
  if (!o || !isOpen(o)) return;
  $("#actTitle").innerHTML = `<span class="side-${o.side}">${o.side}</span> ${esc(o.symbol)}`;
  $("#actBody").innerHTML = `<div class="kv">
      <div><label>Qty</label>${o.qty}</div><div><label>Type</label>${esc(o.order_type)}</div>
      <div><label>Price</label>${o.price ? inr(o.price) : "MKT"}</div><div><label>Trigger</label>${o.trigger_price ? inr(o.trigger_price) : "–"}</div>
      <div><label>Product</label>${esc(o.product)}</div><div><label>Status</label>${esc(o.status)}</div></div>
    <div class="act-list"><button class="btn block" data-oa="modify">Modify</button><button class="btn block sell" data-oa="cancel">Cancel order</button></div>`;
  $("#actBody").dataset.oid = o.order_id;
  openSheet("#actionSheet");
});
$("#actBody").addEventListener("click", async (e) => {
  const a = e.target.dataset.oa; if (!a) return;
  const o = state.orders.find((x) => x.order_id === $("#actBody").dataset.oid);
  closeSheet("#actionSheet");
  if (a === "modify") return openOrder(o.symbol, o.side, { modify: o });
  await act(() => api("DELETE", "/api/orders/" + o.order_id), "Order cancelled");
  loadOrders(); refresh();
});

/* ---------------- bot ---------------- */
function renderSignals(watch) {
  $("#signalsList").innerHTML = watch.map((w) => `<li>
    <div class="li-row"><span class="sym">${esc(w.symbol)}</span><span>${w.action ? `<span class="sigchip ${w.action}">${w.action}</span> ${signed(w.score)}` : "…"}</span></div>
    <div class="li-sub">${esc(w.reason)}</div></li>`).join("") || `<li class="muted">No stocks in watchlist</li>`;
}
function renderEvents(events) {
  $("#eventsList").innerHTML = events.map((e) => `<li class="${esc(e.level)}"><span class="t">${tm(e.time)}</span>${esc(e.message)}</li>`).join("") || '<li class="muted">No activity yet</li>';
}
$("#btnBot").addEventListener("click", async () => {
  if (state.snap.running) await act(() => api("POST", "/api/bot/stop"), "Bot stopped");
  else {
    if (state.snap.mode === "live" && !confirm("LIVE MODE: the bot will place REAL orders with real money. Start?")) return;
    await act(() => api("POST", "/api/bot/start"), "Bot started");
  }
  refresh();
});
$("#btnKill").addEventListener("click", async () => {
  if (state.snap.kill_switch) { await act(() => api("POST", "/api/kill/reset"), "Kill switch reset"); return refresh(); }
  if (!confirm("KILL SWITCH\n\nCancel all open orders, close ALL intraday positions at market and stop the bot?")) return;
  await act(() => api("POST", "/api/kill"), "All closed. Bot stopped.");
  refresh();
});

const FIELDS = [
  ["Strategy"],
  ["strategy", "Strategy", "select:ensemble,ema_cross,macd,rsi"], ["poll_seconds", "Check every (sec)", "int"],
  ["buy_threshold", "Buy when score ≥", "num"], ["sell_threshold", "Sell when score ≤", "num"],
  ["Position sizing"],
  ["risk_per_trade_pct", "Risk per trade %", "num"], ["max_position_pct", "Max % per stock", "num"],
  ["max_open_positions", "Max open positions", "int"], ["cooldown_minutes", "Re-entry wait (min)", "int"],
  ["Loss-cutting & profit protection"],
  ["stop_loss_pct", "Stop-loss %", "num"], ["take_profit_pct", "Take-profit %", "num"],
  ["breakeven_trigger_pct", "Breakeven after +%", "num"], ["trailing_stop_pct", "Trailing stop %", "num"],
  ["early_exit_on_weak_signal", "Exit losers early", "bool"], ["weak_signal_threshold", "…when score ≤", "num"],
  ["max_hold_minutes", "Exit stale trades (min)", "int"],
  ["Daily limits"],
  ["daily_loss_limit_pct", "Stop after day loss %", "num"], ["daily_profit_target_pct", "Lock profit after %", "num"],
  ["profit_giveback_pct", "Max profit give-back %", "num"], ["no_new_entries_after", "No new trades after", "text"],
  ["square_off_time", "Square off at (IST)", "text"],
];
async function loadSettings() {
  const s = await api("GET", "/api/settings"); state.settings = s;
  $("#settingsFields").innerHTML = FIELDS.map(([k, label, type]) => {
    if (!label) return `<div class="group">${k}</div>`;
    const v = s[k];
    if (type.startsWith("select:")) return `<label>${label}<select name="${k}">${type.slice(7).split(",").map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select></label>`;
    if (type === "bool") return `<label>${label}<select name="${k}"><option value="true" ${v ? "selected" : ""}>Yes</option><option value="false" ${!v ? "selected" : ""}>No</option></select></label>`;
    if (type === "text") return `<label>${label}<input name="${k}" value="${esc(v)}" type="time"></label>`;
    return `<label>${label}<input name="${k}" value="${esc(v)}" type="number" step="any" inputmode="decimal"></label>`;
  }).join("");
}
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const changes = {};
  for (const [k, label, type] of FIELDS) {
    if (!label) continue;
    const raw = e.target.elements[k].value;
    const v = type === "num" ? Number(raw) : type === "int" ? parseInt(raw, 10) : type === "bool" ? raw === "true" : raw;
    if (v !== state.settings[k]) changes[k] = v;
  }
  if (!Object.keys(changes).length) return toast("No changes");
  await act(() => api("PATCH", "/api/settings", changes), "Settings saved");
  loadSettings();
});

$("#btForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = e.submitter; btn.disabled = true; btn.textContent = "Running…";
  try {
    const r = await act(() => api("POST", "/api/backtest", {
      symbol: $("#btSymbol").value.trim().toUpperCase(), strategy: $("#btStrategy").value, capital: Number($("#btCapital").value) || null,
    }));
    $("#btResult").classList.remove("hidden");
    $("#btStats").innerHTML = [
      ["Return", signed(r.return_pct) + "%"], ["End equity", rupee(r.end_equity)], ["Trades", r.trades],
      ["Win rate", r.win_rate_pct + "%"], ["Profit factor", r.profit_factor ?? "–"], ["Max drawdown", `<span class="down">${r.max_drawdown_pct}%</span>`],
    ].map(([l, v]) => `<div><label>${l}</label><b>${v}</b></div>`).join("");
    const pts = []; let last = 0;
    for (const c of r.equity_curve) { const t = isoToChart(c.time.replace(" ", "T")); if (t > last) { pts.push({ time: t, value: c.equity }); last = t; } }
    charts.btLine?.setData(pts); charts.bt?.timeScale().fitContent();
    $("#btTrades").innerHTML = r.trade_list.slice().reverse().map((t) => `<li><div class="li-row"><span>${esc(dt(t.entry_time))} · ${t.qty} @ ${inr(t.entry)} → ${inr(t.exit)}</span><b>${signed(t.pnl)}</b></div><div class="li-sub">${esc(t.reason)}</div></li>`).join("") || '<li class="muted">No trades in this period</li>';
  } finally { btn.disabled = false; btn.textContent = "Run backtest"; }
});

/* ---------------- account ---------------- */
async function loadFunds() {
  const f = await api("GET", "/api/funds");
  $("#fAvail").textContent = rupee(f.available);
  $("#fundStats").innerHTML = [
    ["Account balance", rupee(f.cash)], ["Used margin", rupee(f.used_margin)],
    ["Unrealised P&L", signed(f.unrealized)], ["Realised today", signed(f.realized_today)], ["Charges today", rupee(f.charges_today)], ["Bot capital", rupee(f.bot_capital)],
  ].map(([l, v]) => `<div><label>${l}</label><b>${v}</b></div>`).join("");
  $("#botCap").value = f.bot_capital;
  $$("#fundForm button, #fundForm input").forEach((b) => (b.disabled = !f.can_add_funds));
  $("#fundHint").textContent = f.can_add_funds ? "Paper account: add or withdraw any amount."
    : "Live account: add or withdraw money in the Zerodha Kite app. The balance here is your real Kite margin.";
  $("#ledger").innerHTML = [...(f.ledger || [])].reverse().map((r) => `<li><div class="li-row"><span>${esc(r.type)} ${r.note ? `<span class="muted">· ${esc(r.note)}</span>` : ""}</span><b>${r.type === "DEPOSIT" ? signed(r.amount) : signed(-r.amount)}</b></div><div class="li-row li-sub"><span>${esc(dt(r.time))}</span><span>Bal ${inr(r.balance)}</span></div></li>`).join("") || '<li class="muted">No fund movements</li>';
  const s = state.settings || {};
  $("#kiteInfo").textContent = s.kite_configured
    ? (state.snap?.connected && s.data_source === "kite" ? "Connected. Kite login expires daily at ~6 AM." : "Log in each morning to connect your Zerodha account.")
    : "Not set up. Add KITE_API_KEY and KITE_API_SECRET to .env to connect your Zerodha account.";
  $("#btnKite").classList.toggle("hidden", !s.kite_configured);
  loadPreflight();
}
async function loadPreflight() {
  try {
    const checks = await api("GET", "/api/preflight");
    $("#preflight").innerHTML = checks.map((c) => `<li><span>${c.ok === true ? "✅" : c.ok === false ? "❌" : "⚠️"}</span>
      <b>${esc(c.name)}</b><span class="d">${esc(c.detail)}</span></li>`).join("");
  } catch (e) { $("#preflight").innerHTML = `<li class="muted">${esc(e.message)}</li>`; }
}
$("#fundForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const action = e.submitter?.dataset.act || "add";
  const amount = Number($("#fundAmount").value);
  await act(() => api("POST", `/api/funds/${action}`, { amount, note: $("#fundNote").value }), `${action === "add" ? "Added" : "Withdrew"} ${rupee(amount)}`);
  $("#fundAmount").value = ""; $("#fundNote").value = ""; loadFunds(); refresh();
});
$("#botCapForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  await act(() => api("PATCH", "/api/settings", { bot_capital: Number($("#botCap").value) }), "Bot capital saved");
  loadFunds();
});
$("#btnKite").addEventListener("click", async () => { const { url } = await act(() => api("GET", "/api/kite/login")); location.href = url; });

/* ---------------- navigation ---------------- */
function show(view) {
  state.view = view;
  $$("#tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  window.scrollTo(0, 0);
  loadView();
}
$("#tabbar").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) show(b.dataset.view); });
$$(".seg-tabs").forEach((seg) => seg.addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.seg[seg.dataset.seg] = b.dataset.k;
  $$("button", seg).forEach((x) => x.classList.toggle("on", x === b));
  loadView();
}));
async function loadView() {
  const s = state.snap;
  try {
    if (state.view === "orders") await loadOrders();
    if (state.view === "portfolio") { if (state.seg.portfolio === "positions") { if (s) renderPositions(s.positions); } else await loadHoldings(); }
    if (state.view === "bot") { if (s) { renderSignals(s.watchlist); renderEvents(s.events); } loadPnl(); if (!state.settingsLoaded) { await loadSettings(); state.settingsLoaded = true; } }
    if (state.view === "account") await loadFunds();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- order sheet ---------------- */
const form = $("#orderForm");
function openOrder(symbol, side, opts = {}) {
  const m = opts.modify;
  state.order = { symbol, side, modify: m };
  form.reset();
  $("#owSymbol").textContent = symbol;
  $("#owToggle").checked = side === "SELL";
  form.elements.exchange.value = m?.exchange || opts.exchange || "NSE";
  form.elements.product.value = m?.product || opts.product || "MIS";
  form.elements.order_type.value = m?.order_type || "MARKET";
  form.elements.validity.value = m?.validity || "DAY";
  form.elements.qty.value = m?.qty || 1;
  form.elements.price.value = m?.price || "";
  form.elements.trigger_price.value = m?.trigger_price || "";
  form.elements.stop_loss_pct.placeholder = state.settings?.stop_loss_pct ?? 1.5;
  form.elements.target_pct.placeholder = state.settings?.take_profit_pct ?? 3;
  $$("#owExchange input, #owProduct input, #owValidity input").forEach((i) => (i.disabled = !!m));
  $("#owToggle").disabled = !!m;
  $("#owProtect").classList.toggle("hidden", !!m);
  $("#owError").textContent = "";
  syncOrderForm();
  openSheet("#orderSheet");
}
function currentLtp() {
  const o = state.order; if (!o) return null;
  const ex = form.elements.exchange.value;
  const w = state.snap?.watchlist.find((x) => x.symbol === o.symbol && x.exchange === ex);
  const p = state.snap?.positions.find((x) => x.symbol === o.symbol && x.exchange === ex);
  return w?.ltp ?? p?.ltp ?? null;
}
function updateOrderInfo() {
  const ltp = currentLtp();
  $("#owLtp").textContent = `${form.elements.exchange.value} · LTP ${ltp ? "₹" + inr(ltp) : "—"}`;
  const qty = Number(form.elements.qty.value) || 0, t = form.elements.order_type.value;
  const px = (t === "LIMIT" || t === "SL") ? Number(form.elements.price.value) || ltp : ltp;
  $("#owMargin").textContent = px ? `Approx. order value ₹${inr(qty * px)}` : "";
}
function syncOrderForm() {
  const side = $("#owToggle").checked ? "SELL" : "BUY";
  state.order.side = side;
  $("#owSide").textContent = (state.order.modify ? "Modify " : "") + side;
  $("#owHead").classList.toggle("sell", side === "SELL");
  const btn = $("#owSubmit");
  btn.textContent = state.order.modify ? "Modify" : side === "BUY" ? "Buy" : "Sell";
  btn.className = "btn block big " + (side === "BUY" ? "buy" : "sell");
  const t = form.elements.order_type.value;
  form.elements.price.disabled = !(t === "LIMIT" || t === "SL");
  form.elements.trigger_price.disabled = !(t === "SL" || t === "SL-M");
  form.elements.stop_loss_pct.disabled = form.elements.target_pct.disabled = !form.elements.auto_protect.checked;
  if (!form.elements.price.disabled && !Number(form.elements.price.value) && currentLtp()) form.elements.price.value = currentLtp();
  updateOrderInfo();
}
form.addEventListener("input", syncOrderForm);
$("#owCancel").addEventListener("click", () => closeSheet("#orderSheet"));
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form.elements, o = state.order;
  const num = (el) => (el.disabled || el.value === "" ? 0 : Number(el.value));
  try {
    let r;
    if (o.modify) {
      r = await api("PUT", "/api/orders/" + o.modify.order_id, { qty: Number(f.qty.value), price: num(f.price), trigger_price: num(f.trigger_price), order_type: f.order_type.value });
    } else {
      if (state.snap.mode === "live" && !confirm(`LIVE ORDER\n\n${o.side} ${f.qty.value} ${o.symbol}. Place real order?`)) return;
      r = await api("POST", "/api/orders", {
        symbol: o.symbol, side: o.side, qty: Number(f.qty.value), exchange: f.exchange.value, product: f.product.value,
        order_type: f.order_type.value, price: num(f.price), trigger_price: num(f.trigger_price), validity: f.validity.value,
        auto_protect: f.auto_protect.checked,
        stop_loss_pct: f.stop_loss_pct.value ? Number(f.stop_loss_pct.value) : null,
        target_pct: f.target_pct.value ? Number(f.target_pct.value) : null,
      });
    }
    if (r.status === "REJECTED") { $("#owError").textContent = "Rejected: " + (r.status_message || ""); return; }
    toast(`${o.side} ${o.symbol}: ${r.status}`);
    closeSheet("#orderSheet"); refresh(); loadView();
  } catch (err) { $("#owError").textContent = err.message; }
});

/* ---------------- live updates ---------------- */
async function refresh() { try { render(await api("GET", "/api/status")); } catch (e) { /* server restarting */ } }
function connectWS() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = (m) => render(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connectWS, 3000);
}
setInterval(() => {
  if (document.hidden) return;
  if (state.chartSym) loadChart();
  if (state.view === "orders") loadOrders();
  if (state.view === "bot") loadPnl();
}, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

(async function init() {
  try { initCharts(); } catch (e) { console.warn("charts unavailable", e); }
  try { state.settings = await api("GET", "/api/settings"); } catch {}
  await refresh();
  connectWS();
})();
