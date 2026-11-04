"use strict";
// Read-only trading console. All API text (including AI reasoning) is inserted with
// textContent / createElement, never innerHTML.

const REFRESH_MS = 30000;
const TZ = "America/New_York";
const $ = (sel) => document.querySelector(sel);

const PAGES = {
  overview: ["Overview", "Account, bot health and today's risk at a glance"],
  trades: ["Trades", "Completed round trips, matched from broker fills"],
  activity: ["Bot activity", "Every signal, AI decision, risk check and order, newest first"],
  orders: ["Orders", "All orders at the broker, with bracket legs"],
  performance: ["Performance", "How the strategy is doing over time"],
  risk: ["Risk & settings", "Limits the bot cannot override, and how it is configured"],
};

const state = {
  page: "overview", summary: null, decisions: [], trades: null, orders: null,
  eqRange: "1M", trDays: 30, orDays: 7, orStatus: "", pfDays: 30,
};

// ---------- Formatting ----------
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const fmtDT = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const fmtT = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
const fmtD = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });
const fmtWD = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const etDate = (d = new Date()) => new Intl.DateTimeFormat("en-CA", { timeZone: TZ }).format(d);

const money = (v) => (v == null ? "-" : usd.format(v));
const signed = (v) => (v == null ? "-" : (v > 0 ? "+" : "") + usd.format(v));
const pct = (v, d = 2) => (v == null ? "-" : (v > 0 ? "+" : "") + Number(v).toFixed(d) + "%");
const num = (v, d = 2) => (v == null ? "-" : Number(v).toFixed(d));
const qty = (v) => (v == null ? "-" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 4 }));
const when = (iso) => (iso ? fmtDT.format(new Date(iso)) : "-");
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

function duration(seconds) {
  if (seconds == null) return "-";
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
}

// ---------- DOM helpers ----------
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.cls) node.className = opts.cls;
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.title) node.title = opts.title;
  if (opts.href) node.href = opts.href;
  for (const c of children) if (c != null) node.append(c);
  return node;
}
const tag = (text, cls) => el("span", { cls: "tag " + (cls || String(text).toLowerCase()), text });

function setChip(node, text, cls) {
  node.textContent = text;
  node.className = "chip" + (cls ? " " + cls : "");
}

function fillTable(selector, rows, emptyText) {
  const tbody = $(selector + " tbody");
  const cols = $(selector + " thead tr").children.length;
  tbody.replaceChildren();
  if (!rows.length) {
    const td = el("td", { cls: "empty-row", text: emptyText });
    td.colSpan = cols;
    tbody.append(el("tr", {}, [td]));
    return;
  }
  for (const row of rows) {
    const tr = el("tr", { cls: row.cls || "" });
    for (const cell of row.cells) {
      const [content, cls] = Array.isArray(cell) ? cell : [cell, ""];
      const td = el("td", { cls });
      td.append(content instanceof Node ? content : document.createTextNode(String(content)));
      tr.append(td);
    }
    tbody.append(tr);
  }
}

function facts(container, pairs) {
  container.replaceChildren();
  for (const [k, v, cls] of pairs) container.append(el("dt", { text: k }), el("dd", { text: v, cls }));
}

function stats(container, items) {
  container.replaceChildren();
  for (const [label, value, cls, hint] of items) {
    container.append(el("div", { cls: "stat", title: hint }, [el("span", { cls: "k-label", text: label }), el("span", { cls: "s-value " + (cls || ""), text: value })]));
  }
}

function meter(container, { label, used, limit, unit = "", note = "", na = false }) {
  const ratio = na || !limit ? 0 : Math.min(1, used / limit);
  const bar = el("div", { cls: "bar" + (na ? " na" : ratio >= 0.9 ? " high" : ratio >= 0.6 ? " mid" : "") });
  const fill = el("span");
  fill.style.width = (na ? 0 : Math.max(ratio * 100, used > 0 ? 3 : 0)) + "%";
  bar.append(fill);
  const right = na ? "not applicable" : `${num(used, unit === "%" ? 2 : 0)}${unit} of ${num(limit, unit === "%" ? 2 : 0)}${unit}`;
  const block = el("div", {}, [el("div", { cls: "meter-top" }, [el("span", { text: label }), el("span", { text: right })]), bar]);
  if (note) block.append(el("div", { cls: "meter-note", text: note }));
  container.append(block);
}

// ---------- API ----------
async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ---------- Shared: summary, top bar, banners ----------
const GATES = {
  ok: ["Scanning for setups", "The market is open and the bot checks every symbol at each bar close.", "ok"],
  market_closed: ["Waiting for the open", "The market is closed. The bot checks the clock and wakes up at the open.", ""],
  opening_blackout: ["Opening blackout", "No new trades in the first minutes after the open while prices settle.", "warn"],
  eod_flatten: ["Closing for the day", "Near the close: new trades are blocked and all positions are being closed.", "warn"],
  kill_switch: ["Halted for today", "The daily loss limit was hit. Positions were closed; no new trades until tomorrow.", "bad"],
  broker_unavailable: ["Broker unreachable", "The bot could not reach Alpaca and is skipping trades until it can.", "bad"],
};

function marketText(clock) {
  if (!clock) return ["Market", ""];
  if (clock.is_open) {
    const left = (new Date(clock.next_close) - Date.now()) / 1000;
    return [`Market open · closes in ${duration(left)}`, "ok"];
  }
  const until = (new Date(clock.next_open) - Date.now()) / 1000;
  return [until < 86400 ? `Market closed · opens in ${duration(until)}` : `Market closed · opens ${fmtWD.format(new Date(clock.next_open))} ET`, ""];
}

function renderTop(s) {
  const live = s.mode === "live";
  setChip($("#mode"), live ? "LIVE" : "Paper", live ? "bad" : "info");
  $("#side-mode").textContent = live ? "live account" : "paper account";
  const [mt, mc] = marketText(s.clock);
  setChip($("#market"), mt, mc);

  const bot = s.bot;
  const running = bot.state === "running";
  $("#side-bot").textContent = running ? "Bot running" : bot.state === "stale" ? "Bot not reporting" : "No bot data yet";
  $("#side-bot-dot").className = "dot " + (running ? "ok" : "bad");

  const halt = $("#banner-halt");
  if (s.account.trading_blocked) {
    halt.textContent = "The broker reports that trading is blocked on this account.";
    halt.hidden = false;
  } else if (s.risk.halted_today) {
    halt.textContent = `Kill switch active: ${s.halt.reason}. All positions were closed and no new trades will be placed today.`;
    halt.hidden = false;
  } else {
    halt.hidden = true;
  }

  const sel = $("#f-symbol");
  if (sel.options.length === 1) for (const sym of s.symbols) sel.append(el("option", { text: sym }));
}

async function loadSummary() {
  state.summary = await api("api/summary");
  renderTop(state.summary);
  return state.summary;
}

// ---------- Overview ----------
function renderOverview(s) {
  const a = s.account;
  $("#k-equity").textContent = money(a.equity);
  $("#k-equity-sub").textContent = `Buying power ${usd0.format(a.buying_power || 0)}`;
  $("#k-equity-sub").title = `Cash ${money(a.cash)}`;
  const day = $("#k-day");
  day.textContent = signed(a.day_pl);
  day.className = "k-value " + tone(a.day_pl);
  $("#k-day-sub").textContent = `${pct(a.day_pl_pct)} vs. yesterday's close`;

  const openPl = s.positions.reduce((t, p) => t + (p.unrealized_pl || 0), 0);
  const open = $("#k-open");
  open.textContent = s.positions.length ? signed(openPl) : "-";
  open.className = "k-value " + tone(openPl);
  $("#k-open-sub").textContent = s.positions.length ? `${s.positions.length} open position${s.positions.length > 1 ? "s" : ""}` : "No open positions";

  const r = s.risk;
  $("#k-today").textContent = `${r.trades_today.used} / ${r.trades_today.limit}`;
  $("#k-today-sub").textContent = r.trades_today.used >= r.trades_today.limit ? "Daily trade limit reached" : `${r.trades_today.limit - r.trades_today.used} entries left today`;

  // Bot status card
  const bot = s.bot;
  const [head, explain, cls] = bot.state === "running"
    ? GATES[bot.gate] || [bot.gate, "", ""]
    : bot.state === "stale"
      ? ["Bot is not reporting", `No heartbeat for ${duration(bot.age_s)}. Check the service on the server: journalctl -u trading-bot -f`, "bad"]
      : ["No data from the bot yet", "The bot has not written a heartbeat. Is the trading-bot service running?", "bad"];
  setChip($("#bot-chip"), bot.state === "running" ? "Running" : bot.state === "stale" ? "Not reporting" : "Unknown", bot.state === "running" ? cls || "ok" : "bad");
  $("#bot-headline").textContent = head;
  $("#bot-explain").textContent = explain;
  facts($("#bot-facts"), [
    ["Last heartbeat", bot.last_heartbeat ? `${when(bot.last_heartbeat)} ET (${duration(bot.age_s)} ago)` : "-"],
    ["Uptime", duration(bot.uptime_s)],
    ["Cycle", bot.cycle != null ? `#${bot.cycle} · ${bot.last_cycle_ms ?? "-"} ms` : "-"],
    ["Errors (24h)", String(bot.errors_24h ?? 0), bot.errors_24h ? "warn-text" : ""],
    ["Scan interval", `every ${s.timeframe_minutes} min`],
  ]);

  // Risk meters
  const m = $("#meters");
  m.replaceChildren();
  meter(m, { label: "Daily loss", used: r.daily_loss.used, limit: r.daily_loss.limit, unit: "%", note: "Kill switch closes everything when this bar is full." });
  meter(m, { label: "Open positions", used: r.positions.used, limit: r.positions.limit });
  meter(m, { label: "Trades today", used: r.trades_today.used, limit: r.trades_today.limit });
  meter(m, { label: "Day trades (5 sessions)", used: r.day_trades.used, limit: r.day_trades.limit, na: !r.day_trades.applies,
             note: r.day_trades.applies ? "Pattern-day-trader rule applies below " + usd0.format(r.day_trades.threshold) + " equity." : "Equity is above the PDT threshold." });

  // Positions
  $("#pos-meta").textContent = s.positions.length ? `Protected by bracket stop-loss / take-profit orders` : "";
  fillTable("#positions", s.positions.map((p) => ({
    cells: [[el("span", { cls: "sym", text: p.symbol })], [tag(p.side)], [qty(p.qty), "r"], [num(p.avg_entry_price), "r"],
      [num(p.current_price), "r"], [money(p.market_value), "r"],
      [`${signed(p.unrealized_pl)} (${pct(p.unrealized_plpc == null ? null : p.unrealized_plpc * 100)})`, "r " + tone(p.unrealized_pl)]],
  })), "No open positions. The bot is flat.");
}

function renderWinKpi(data) {
  const st = data.stats;
  $("#k-win").textContent = st.win_rate == null ? "-" : `${st.win_rate}%`;
  $("#k-win-sub").textContent = st.trades ? `${st.wins}W / ${st.losses}L · net ${signed(st.net_pnl)}` : "No closed trades yet";
}

// ---------- Charts ----------
const NS = "http://www.w3.org/2000/svg";
function svg(tagName, attrs = {}) {
  const n = document.createElementNS(NS, tagName);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}
function svgText(x, y, text, anchor = "end") {
  const t = svg("text", { x, y, "text-anchor": anchor, class: "axis" });
  t.textContent = text;
  return t;
}
function chartFrame(box) {
  box.replaceChildren();
  return { W: box.clientWidth || 800, H: box.clientHeight || 260 };
}
function attachTip(box, root, W, onMove) {
  const tip = el("div", { cls: "tip" });
  tip.hidden = true;
  box.append(tip);
  root.addEventListener("pointermove", (ev) => {
    const rect = root.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    const res = onMove(px);
    if (!res) return;
    tip.textContent = res.text;
    tip.hidden = false;
    const left = (res.x / W) * rect.width;
    tip.style.left = Math.min(Math.max(left - tip.offsetWidth / 2, 0), rect.width - tip.offsetWidth) + "px";
  });
  root.addEventListener("pointerleave", () => { tip.hidden = true; onMove(null); });
}

function renderEquity(data) {
  const box = $("#eq-chart");
  const { W, H } = chartFrame(box);
  const pts = data.points.filter((p) => p.equity > 0);
  if (pts.length < 2) { box.append(el("div", { cls: "empty", text: "Not enough history yet. The curve appears after a few days of trading." })); return; }
  const L = 70, R = 10, T = 12, B = 26;
  const ys = pts.map((p) => p.equity);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (data.base_value) { lo = Math.min(lo, data.base_value); hi = Math.max(hi, data.base_value); }
  const pad = (hi - lo) * 0.1 || hi * 0.002 || 1;
  lo -= pad; hi += pad;
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}` });
  for (let i = 0; i <= 4; i++) {
    const v = lo + ((hi - lo) * i) / 4;
    root.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }), svgText(L - 8, y(v) + 4, usd0.format(v)));
  }
  const intraday = data.period === "1D";
  const xf = intraday ? fmtT : fmtD;
  const ticks = [0, Math.floor((pts.length - 1) / 2), pts.length - 1];
  ticks.forEach((i, k) => root.append(svgText(x(i), H - 6, xf.format(new Date(pts[i].t)), k === 0 ? "start" : k === 2 ? "end" : "middle")));
  if (data.base_value) root.append(svg("line", { x1: L, x2: W - R, y1: y(data.base_value), y2: y(data.base_value), class: "base" }));
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  root.append(svg("path", { d: `${d}L${x(pts.length - 1)},${H - B}L${L},${H - B}Z`, class: "area" }), svg("path", { d, class: "line" }));
  const cursor = svg("line", { y1: T, y2: H - B, class: "cursor", visibility: "hidden" });
  const dot = svg("circle", { r: 4, class: "dot", visibility: "hidden" });
  root.append(cursor, dot);
  box.prepend(root);
  attachTip(box, root, W, (px) => {
    if (px == null) { cursor.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); return null; }
    const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - L) / (W - L - R)) * (pts.length - 1))));
    const p = pts[i];
    cursor.setAttribute("x1", x(i)); cursor.setAttribute("x2", x(i)); cursor.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.equity)); dot.setAttribute("visibility", "visible");
    return { x: x(i), text: `${(intraday ? fmtDT : fmtD).format(new Date(p.t))} · ${money(p.equity)} · ${signed(p.pl)}` };
  });
}

function renderDailyBars(daily) {
  const box = $("#pf-chart");
  const { W, H } = chartFrame(box);
  if (!daily.length) { box.append(el("div", { cls: "empty", text: "No daily results in this period yet" })); return; }
  const L = 70, R = 10, T = 12, B = 26;
  const vals = daily.map((d) => d.pnl);
  const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals);
  const span = hi - lo || 1;
  const y = (v) => T + (1 - (v - lo) / span) * (H - T - B);
  const slot = (W - L - R) / daily.length;
  const bw = Math.max(2, Math.min(28, slot * 0.7));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}` });
  for (const v of [hi, lo]) if (v !== 0) root.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }), svgText(L - 8, y(v) + 4, usd0.format(v)));
  root.append(svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), class: "zero" }), svgText(L - 8, y(0) + 4, "$0"));
  daily.forEach((d, i) => {
    const cx = L + slot * i + slot / 2;
    const top = y(Math.max(d.pnl, 0)), bottom = y(Math.min(d.pnl, 0));
    root.append(svg("rect", { x: cx - bw / 2, y: top, width: bw, height: Math.max(1, bottom - top), rx: 2, class: d.pnl >= 0 ? "bar-up" : "bar-down" }));
  });
  const label = (d) => fmtD.format(new Date(d.date + "T12:00:00Z"));
  root.append(svgText(L, H - 6, label(daily[0]), "start"), svgText(W - R, H - 6, label(daily[daily.length - 1]), "end"));
  box.prepend(root);
  attachTip(box, root, W, (px) => {
    if (px == null) return null;
    const i = Math.max(0, Math.min(daily.length - 1, Math.floor((px - L) / slot)));
    const d = daily[i];
    return { x: L + slot * i + slot / 2, text: `${label(d)} · ${signed(d.pnl)} (${pct(d.pct)})` };
  });
}

// ---------- Activity ----------
const KINDS = {
  "signal": ["Signal", ""], "ai.decision": ["AI", "ai"], "ai.invalid_response_final": ["AI invalid", "bad"],
  "ai.all_providers_failed": ["AI down", "bad"], "risk.approved": ["Approved", "ok"], "risk.rejected": ["Rejected", "bad"],
  "risk.kill_switch": ["Kill switch", "bad"], "risk.force_flatten": ["Flatten", "bad"], "order.submitted": ["Order", "ok"],
  "order.recovered_after_error": ["Order", "ok"], "symbol.skipped": ["Skipped", ""],
};
const PROBLEMS = new Set(["ai.invalid_response_final", "ai.all_providers_failed", "risk.kill_switch", "risk.force_flatten", "symbol.skipped"]);
const REASONS = {
  pdt_limit: "day-trade limit (PDT)", reward_risk_too_low: "reward:risk too low", stop_too_tight: "stop too close (vs. ATR)",
  low_confidence: "AI confidence too low", max_concurrent_positions: "too many open positions", max_trades_per_day: "daily trade limit reached",
  duplicate_symbol: "already holding this symbol", size_below_one: "position would be under 1 share", eod_window: "too close to the close",
  opening_blackout: "too soon after the open", kill_switch_active: "kill switch active", shorting_disabled: "short selling disabled",
  invalid_stop_side: "stop on the wrong side", invalid_target_side: "target on the wrong side", broker_unavailable: "broker unreachable",
};

function describe(e) {
  switch (e.event) {
    case "signal":
      return [`${e.action} signal · confidence ${num(e.confidence)}` + (e.entry ? ` · entry ${num(e.entry)}, stop ${num(e.stop)}, target ${num(e.target)}` : ""), e.reasoning];
    case "ai.decision":
      return [`${e.model} says ${e.action} · confidence ${num(e.confidence)}`, e.reasoning];
    case "risk.rejected":
      return [`${e.action} blocked: ${REASONS[e.reason] || String(e.reason).replaceAll("_", " ")}`, e.detail];
    case "risk.approved":
      return [`${String(e.side).toUpperCase()} ${e.qty} @ ${num(e.entry_price)} · stop ${num(e.stop_loss)} · target ${num(e.take_profit)}`, `Risking ${money(e.risk_dollars)} on ${money(e.notional)}`];
    case "order.submitted":
      return [`Order ${e.status}: ${String(e.side).toUpperCase()} ${qty(e.qty)}` + (e.dry_run ? " (dry run)" : ""), e.client_order_id];
    case "risk.kill_switch":
      return [`Daily loss ${e.drawdown_pct}%: kill switch fired`, `Equity ${money(e.equity)} vs. ${money(e.start_equity)} at the open`];
    case "risk.force_flatten":
      return [`Closing all positions (${String(e.reason).replaceAll("_", " ")})`, ""];
    case "symbol.skipped":
      return [`Skipped this cycle: ${e.error_type}`, e.error];
    default:
      return [e.error || e.event, e.raw || ""];
  }
}

function feedItem(e, compact) {
  const [kindText, kindCls] = KINDS[e.event] || [e.event, ""];
  const [headline, why] = describe(e);
  const what = el("div", { cls: "what" }, [el("span", { cls: "kind " + kindCls, text: kindText }), document.createTextNode(headline)]);
  if (why) what.append(el("span", { cls: "why", text: why, title: compact ? why : "" }));
  const d = new Date(e.ts);
  const t = compact || etDate(d) === etDate() ? fmtT.format(d) : fmtDT.format(d);
  return el("li", {}, [el("span", { cls: "time", text: t }), el("span", { cls: "s", text: e.symbol || "-" }), what]);
}

function renderFeed(list, events, compact, emptyText) {
  list.replaceChildren();
  if (!events.length) { list.append(el("li", {}, [el("span", { cls: "empty-li", text: emptyText })])); return; }
  for (const e of events) list.append(feedItem(e, compact));
}

function renderActivity() {
  const sym = $("#f-symbol").value, type = $("#f-type").value, q = $("#f-search").value.trim().toLowerCase();
  const rows = state.decisions.filter((e) =>
    (!sym || e.symbol === sym) &&
    (!type || (type === "problems" ? PROBLEMS.has(e.event) : e.event === type)) &&
    (!q || describe(e).join(" ").toLowerCase().includes(q)));
  $("#act-meta").textContent = `${rows.length} of ${state.decisions.length} recent events`;
  renderFeed($("#activity"), rows, false, "No matching bot activity. Activity appears once the market is open.");
}

// ---------- Trades ----------
function renderTradesPage(data) {
  state.trades = data;
  const s = data.stats;
  stats($("#tr-stats"), [
    ["Trades", String(s.trades)], ["Win rate", s.win_rate == null ? "-" : s.win_rate + "%"],
    ["Net P&L", signed(s.net_pnl), tone(s.net_pnl)], ["Profit factor", s.profit_factor ?? "-", "", "Gross profit / gross loss. Above 1 = profitable."],
    ["Expectancy", signed(s.expectancy), tone(s.expectancy), "Average P&L per trade"], ["Avg win", money(s.avg_win), "up"],
    ["Avg loss", money(s.avg_loss), "down"], ["Avg hold", s.avg_hold_min == null ? "-" : duration(s.avg_hold_min * 60)],
  ]);
  $("#tr-meta").textContent = `Last ${data.days} days`;
  fillTable("#trades", data.trades.map((t) => {
    const held = (new Date(t.closed_at) - new Date(t.opened_at)) / 1000;
    return { cells: [when(t.closed_at), [el("span", { cls: "sym", text: t.symbol })], [tag(t.direction)], [qty(t.qty), "r"],
      [num(t.entry_price), "r"], [num(t.exit_price), "r"], [duration(held), "r"], [signed(t.pnl), "r " + tone(t.pnl)], [pct(t.return_pct), "r " + tone(t.pnl)]] };
  }), "No closed trades in this period.");
}

function exportCsv() {
  const trades = (state.trades && state.trades.trades) || [];
  const head = ["closed_at", "opened_at", "symbol", "direction", "qty", "entry_price", "exit_price", "pnl", "return_pct"];
  const esc = (v) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s; };
  const csv = [head.join(","), ...trades.map((t) => head.map((k) => esc(t[k])).join(","))].join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  const a = el("a", { href: url });
  a.download = `trades_${etDate()}_${state.trDays}d.csv`;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------- Orders ----------
const OPEN_STATUSES = new Set(["new", "accepted", "held", "partially_filled", "pending_new", "accepted_for_bidding", "pending_replace"]);
function renderOrders() {
  const data = state.orders;
  if (!data) return;
  const f = state.orStatus;
  const keep = (o) => !f || (f === "open" ? OPEN_STATUSES.has(o.status) : f === "canceled" ? ["canceled", "expired", "rejected"].includes(o.status) : o.status === f);
  const list = data.orders.filter(keep);
  $("#or-meta").textContent = `${list.length} orders · last ${data.days === 1 ? "day" : data.days + " days"}`;
  const rows = [];
  const priceOf = (o) => (o.limit_price != null ? num(o.limit_price) : o.stop_price != null ? num(o.stop_price) : "-");
  for (const o of list) {
    rows.push({ cells: [when(o.submitted_at), [el("span", { cls: "sym", text: o.symbol })], [tag(o.side)], o.order_class === "bracket" ? "bracket" : o.type,
      [qty(o.qty), "r"], [priceOf(o), "r"], [o.filled_avg_price == null ? "-" : num(o.filled_avg_price), "r"], [tag(o.status)],
      [tag(o.by_bot ? "bot" : "manual", o.by_bot ? "open" : "muted")]] });
    for (const leg of o.legs) {
      rows.push({ cls: "leg", cells: ["", `↳ ${leg.type === "stop" ? "stop-loss" : leg.type === "limit" ? "take-profit" : leg.type}`, [tag(leg.side)], leg.type,
        [qty(leg.qty), "r"], [priceOf(leg), "r"], [leg.filled_avg_price == null ? "-" : num(leg.filled_avg_price), "r"], [tag(leg.status)], ""] });
    }
  }
  fillTable("#orders", rows, "No orders match this filter.");
}

// ---------- Performance ----------
function renderPerformance(p) {
  const s = p.stats;
  stats($("#pf-stats"), [
    ["Net P&L", signed(s.net_pnl), tone(s.net_pnl)], ["Win rate", s.win_rate == null ? "-" : `${s.win_rate}%`],
    ["Profit factor", s.profit_factor ?? "-", "", "Gross profit / gross loss"], ["Expectancy", signed(s.expectancy), tone(s.expectancy), "Average P&L per trade"],
    ["Max drawdown", p.max_drawdown_pct == null ? "-" : `-${num(p.max_drawdown_pct)}%`, p.max_drawdown_pct ? "down" : "", "Largest peak-to-trough equity drop"],
    ["Best trade", signed(s.best), tone(s.best)], ["Worst trade", signed(s.worst), tone(s.worst)],
    ["Green / red days", `${p.green_days} / ${p.red_days}`],
  ]);
  $("#pf-days").textContent = `${p.daily.length} trading days`;
  renderDailyBars(p.daily);
  fillTable("#by-symbol", p.by_symbol.map((r) => ({ cells: [[el("span", { cls: "sym", text: r.symbol })], [String(r.trades), "r"], [r.win_rate + "%", "r"], [signed(r.net_pnl), "r " + tone(r.net_pnl)]] })),
    "No closed trades in this period.");
}

// ---------- Risk & settings ----------
function renderRisk(s, cfg, ai) {
  const r = s.risk;
  setChip($("#ks-chip"), r.halted_today ? "Triggered" : "Armed", r.halted_today ? "bad" : "ok");
  $("#ks-text").textContent = r.halted_today
    ? `Fired today: ${s.halt.reason}. Trading resumes automatically next session.`
    : `If equity falls ${num(r.daily_loss.limit)}% below yesterday's close, the bot cancels all orders, closes every position and stops for the day.`;
  const km = $("#ks-meter");
  km.replaceChildren();
  meter(km, { label: "Loss so far today", used: r.daily_loss.used, limit: r.daily_loss.limit, unit: "%" });

  const box = $("#ai-facts");
  box.replaceChildren(el("span"), el("span", { cls: "hdr", text: "Today" }), el("span", { cls: "hdr", text: "7 days" }));
  const cost = (v) => (v == null ? "n/a" : "$" + Number(v).toFixed(v < 1 ? 4 : 2));
  for (const [k, f] of [
    ["AI calls", (u) => String(u.calls)], ["Tokens in / out", (u) => `${u.prompt_tokens.toLocaleString()} / ${u.completion_tokens.toLocaleString()}`],
    ["Est. cost", (u) => cost(u.cost_usd)], ["Avg latency", (u) => (u.avg_latency_ms == null ? "-" : (u.avg_latency_ms / 1000).toFixed(1) + " s")],
    ["Failures", (u) => String(u.failures)],
  ]) box.append(el("dt", { text: k }), el("dd", { text: f(ai.today) }), el("dd", { text: f(ai.week) }));

  fillTable("#limits", cfg.limits.map((l) => ({ cells: [l.rule, [String(l.value), "r"], [l.hard == null ? "configurable" : `${l.kind} ${l.hard}`, "r"], l.unit] })), "-");
  const t = cfg.trading;
  facts($("#cfg-trading"), [
    ["Mode", t.mode === "live" ? "LIVE" : "Paper", t.mode === "live" ? "down" : ""], ["Strategy", t.strategy],
    ["Bar size", `${t.timeframe_minutes} min`], ["Data feed", t.data_feed.toUpperCase()],
    ["Order-flow proxy", t.cvd_source === "trades" ? "trade prints (tick rule)" : "bars"], ["Short selling", t.allow_short ? "allowed" : "off"],
    ["AI model", cfg.ai.model], ["Fallback models", cfg.ai.fallback_models.join(", ") || "none"],
    ["Flatten on shutdown", t.flatten_on_shutdown ? "yes" : "no"],
  ]);
  $("#wl-count").textContent = `${t.symbols.length} symbols`;
  $("#watchlist").replaceChildren(...t.symbols.map((sym) => el("span", { cls: "chip plain", text: sym })));
}

// ---------- Page loaders ----------
const LOADERS = {
  async overview() {
    const s = await loadSummary();
    renderOverview(s);
    const [eq, tr, dec] = await Promise.allSettled([api("api/equity?range=" + state.eqRange), api("api/trades?days=30"), api("api/decisions?limit=6")]);
    if (eq.status === "fulfilled") renderEquity(eq.value);
    if (tr.status === "fulfilled") renderWinKpi(tr.value);
    if (dec.status === "fulfilled") renderFeed($("#recent"), dec.value.events, true, "Nothing yet. Bot decisions appear here during market hours.");
    const failed = [eq, tr, dec].find((r) => r.status === "rejected");
    if (failed) throw failed.reason;
  },
  async trades() { await loadSummary(); renderTradesPage(await api("api/trades?days=" + state.trDays)); },
  async activity() { await loadSummary(); state.decisions = (await api("api/decisions?limit=500")).events; renderActivity(); },
  async orders() { await loadSummary(); state.orders = await api("api/orders?days=" + state.orDays); renderOrders(); },
  async performance() { await loadSummary(); renderPerformance(await api("api/performance?days=" + state.pfDays)); },
  async risk() {
    const [s, cfg, ai] = await Promise.all([loadSummary(), api("api/config"), api("api/ai-usage")]);
    renderRisk(s, cfg, ai);
  },
};

let inflight = false;
async function refresh() {
  if (inflight) return;
  inflight = true;
  $("#refresh").classList.add("spin");
  const err = $("#banner-error");
  try {
    await LOADERS[state.page]();
    err.hidden = true;
  } catch (e) {
    err.textContent = "Some data could not be loaded (" + e.message + "). Retrying automatically.";
    err.hidden = false;
  } finally {
    inflight = false;
    $("#refresh").classList.remove("spin");
    $("#updated").textContent = "Updated " + fmtT.format(new Date()) + " ET";
  }
}

// ---------- Routing & controls ----------
function route() {
  const page = location.hash.slice(1);
  state.page = PAGES[page] ? page : "overview";
  for (const sec of document.querySelectorAll("[data-page]")) sec.hidden = sec.dataset.page !== state.page;
  for (const a of document.querySelectorAll("[data-nav]")) {
    const on = a.dataset.nav === state.page;
    a.classList.toggle("active", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  const [title, sub] = PAGES[state.page];
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
  document.title = `${title} · Trading Console`;
  refresh();
}

function segment(id, onPick) {
  $(id).addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (!btn) return;
    for (const b of $(id).querySelectorAll("button")) b.classList.toggle("on", b === btn);
    onPick(btn.dataset.v);
  });
}

segment("#eq-range", (v) => { state.eqRange = v; api("api/equity?range=" + v).then(renderEquity).catch(() => {}); });
segment("#tr-range", (v) => { state.trDays = Number(v); refresh(); });
segment("#or-range", (v) => { state.orDays = Number(v); refresh(); });
segment("#or-status", (v) => { state.orStatus = v; renderOrders(); });
segment("#pf-range", (v) => { state.pfDays = Number(v); refresh(); });
for (const id of ["#f-symbol", "#f-type"]) $(id).addEventListener("change", renderActivity);
$("#f-search").addEventListener("input", renderActivity);
$("#tr-export").addEventListener("click", exportCsv);
$("#refresh").addEventListener("click", refresh);

// Theme: follows the OS unless toggled; the choice is remembered in this browser only.
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}
try { applyTheme(localStorage.getItem("tdp-theme")); } catch (_) { /* storage unavailable */ }
$("#theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("tdp-theme", next); } catch (_) { /* ignore */ }
});

// Keep the market countdown fresh between data refreshes.
setInterval(() => { if (state.summary) setChip($("#market"), ...marketText(state.summary.clock)); }, 15000);

let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.page === "overview" || state.page === "performance") refresh(); }, 250); });
window.addEventListener("hashchange", route);
setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
route();
