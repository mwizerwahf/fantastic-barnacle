"use strict";
// All text from the API (including AI reasoning) is inserted with textContent, never innerHTML.

const REFRESH_MS = 30000;
const TZ = "America/New_York";
const state = { range: "1M", tab: "trades", decisions: [] };

const $ = (sel) => document.querySelector(sel);
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const dtFmt = new Intl.DateTimeFormat("en-US", {
  timeZone: TZ, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
});
const timeFmt = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
const dayFmt = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });

const money = (v) => (v == null ? "-" : usd.format(v));
const signed = (v) => (v == null ? "-" : (v > 0 ? "+" : "") + usd.format(v));
const pct = (v) => (v == null ? "-" : (v > 0 ? "+" : "") + v.toFixed(2) + "%");
const num = (v, d = 2) => (v == null ? "-" : Number(v).toFixed(d));
const when = (iso) => (iso ? dtFmt.format(new Date(iso)) : "-");
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.cls) node.className = opts.cls;
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.title) node.title = opts.title;
  for (const child of children) node.append(child);
  return node;
}

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function setPill(node, text, cls) {
  node.textContent = text;
  node.className = "pill" + (cls ? " " + cls : "");
}

function fillTable(selector, rows, cols, emptyText) {
  const tbody = $(selector + " tbody");
  tbody.replaceChildren();
  if (!rows.length) {
    const td = el("td", { cls: "empty-row", text: emptyText });
    td.colSpan = cols;
    tbody.append(el("tr", {}, [td]));
    return;
  }
  for (const cells of rows) {
    tbody.append(el("tr", {}, cells.map(([text, cls]) => el("td", { text, cls }))));
  }
}

// --- Summary ---------------------------------------------------------------
function renderSummary(s) {
  const a = s.account;
  setPill($("#mode"), s.mode === "live" ? "LIVE" : "paper", s.mode === "live" ? "bad" : "paper");
  setPill($("#market"), s.clock.is_open ? "market open" : "market closed", s.clock.is_open ? "ok" : "");
  const bot = s.bot;
  const botText = bot.state === "running" ? `bot running · ${bot.gate}` : bot.state === "stale" ? "bot not reporting" : "bot: no data";
  setPill($("#bot"), botText, bot.state === "running" ? "ok" : "bad");
  $("#bot").title = bot.last_heartbeat ? "Last heartbeat " + when(bot.last_heartbeat) + " ET" : "";

  $("#k-equity").textContent = money(a.equity);
  const day = $("#k-daypl");
  day.textContent = `${signed(a.day_pl)} (${pct(a.day_pl_pct)})`;
  day.className = "value " + tone(a.day_pl);
  $("#k-bp").textContent = money(a.buying_power);
  $("#k-dt").textContent = a.daytrade_count ?? "-";

  const halt = $("#halt");
  const todayEt = new Intl.DateTimeFormat("en-CA", { timeZone: TZ }).format(new Date());
  if (s.halt && s.halt.session_date === todayEt) {
    halt.textContent = `Kill switch active for ${s.halt.session_date}: ${s.halt.reason}. No new entries this session.`;
    halt.hidden = false;
  } else {
    halt.hidden = true;
  }
  if (a.trading_blocked) {
    halt.textContent = "Broker reports trading is blocked on this account.";
    halt.hidden = false;
  }

  fillTable("#positions", s.positions.map((p) => [
    [p.symbol], [p.side], [num(p.qty, 0), "r"], [num(p.avg_entry_price), "r"], [num(p.current_price), "r"],
    [money(p.market_value), "r"], [`${signed(p.unrealized_pl)} (${pct(p.unrealized_plpc == null ? null : p.unrealized_plpc * 100)})`, "r " + tone(p.unrealized_pl)],
  ]), 7, "No open positions");

  const sel = $("#f-symbol");
  if (sel.options.length === 1) {
    for (const sym of s.symbols) sel.append(el("option", { text: sym }));
  }
}

// --- Equity chart ------------------------------------------------------------
const SVG = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function renderEquity(data) {
  const box = $("#chart");
  box.replaceChildren();
  const pts = data.points.filter((p) => p.equity > 0);
  if (pts.length < 2) {
    box.append(el("div", { cls: "empty", text: "Not enough equity history yet" }));
    return;
  }
  const W = box.clientWidth || 800, H = box.clientHeight || 240, L = 64, R = 12, T = 10, B = 24;
  const ys = pts.map((p) => p.equity);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (data.base_value) { lo = Math.min(lo, data.base_value); hi = Math.max(hi, data.base_value); }
  const pad = (hi - lo) * 0.08 || hi * 0.001 || 1;
  lo -= pad; hi += pad;
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}` });
  for (let i = 0; i <= 3; i++) {
    const v = lo + ((hi - lo) * i) / 3;
    root.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid" }));
    const label = svg("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end", class: "axis" });
    label.textContent = usd.format(Math.round(v));
    root.append(label);
  }
  const intraday = data.timeframe.endsWith("Min") || data.timeframe.endsWith("H");
  const fmt = intraday && data.period === "1D" ? timeFmt : dayFmt;
  for (const i of [0, Math.floor((pts.length - 1) / 2), pts.length - 1]) {
    const label = svg("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle", class: "axis" });
    label.textContent = fmt.format(new Date(pts[i].t));
    root.append(label);
  }
  if (data.base_value) {
    root.append(svg("line", { x1: L, x2: W - R, y1: y(data.base_value), y2: y(data.base_value), class: "base" }));
  }
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  root.append(svg("path", { d: `${line}L${x(pts.length - 1)},${H - B}L${L},${H - B}Z`, class: "area" }));
  root.append(svg("path", { d: line, class: "line" }));

  const cursor = svg("line", { y1: T, y2: H - B, class: "cursor", visibility: "hidden" });
  const dot = svg("circle", { r: 4, class: "dot", visibility: "hidden" });
  root.append(cursor, dot);
  const tip = el("div", { cls: "tip" });
  tip.hidden = true;
  box.append(root, tip);

  root.addEventListener("pointermove", (ev) => {
    const rect = root.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round(((px - L) / (W - L - R)) * (pts.length - 1))));
    const p = pts[i];
    for (const n of [cursor, dot]) n.setAttribute("visibility", "visible");
    cursor.setAttribute("x1", x(i)); cursor.setAttribute("x2", x(i));
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.equity));
    tip.textContent = `${(intraday ? dtFmt : dayFmt).format(new Date(p.t))} · ${money(p.equity)} · ${signed(p.pl)}`;
    tip.hidden = false;
    const left = (x(i) / W) * rect.width;
    tip.style.left = Math.min(Math.max(left - tip.offsetWidth / 2, 0), rect.width - tip.offsetWidth) + "px";
  });
  root.addEventListener("pointerleave", () => {
    for (const n of [cursor, dot]) n.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
}

// --- Trades / orders / decisions --------------------------------------------------
function renderTrades(data) {
  const s = data.stats;
  const net = $("#k-netpl");
  net.textContent = signed(s.net_pnl);
  net.className = "value " + tone(s.net_pnl);
  $("#k-winrate").textContent = s.win_rate == null ? "-" : `${s.win_rate}% of ${s.trades}`;
  if (state.tab === "trades") {
    $("#tab-meta").textContent = `Last ${data.days} days · profit factor ${s.profit_factor ?? "-"} · avg win ${money(s.avg_win)} · avg loss ${money(s.avg_loss)}`;
  }
  fillTable("#trades", data.trades.map((t) => [
    [when(t.closed_at)], [t.symbol], [t.direction], [num(t.qty, 0), "r"], [num(t.entry_price), "r"],
    [num(t.exit_price), "r"], [signed(t.pnl), "r " + tone(t.pnl)], [pct(t.return_pct), "r " + tone(t.pnl)],
  ]), 8, "No closed trades in this period");
}

function renderOrders(data) {
  if (state.tab === "orders") $("#tab-meta").textContent = `Last ${data.days} days · bracket legs shown under each entry`;
  const rows = [];
  for (const o of data.orders) {
    const push = (order, child) => rows.push([
      [child ? "" : when(order.submitted_at)], [child ? "  ↳ " + order.symbol : order.symbol], [order.side],
      [child ? `${order.type} leg` : order.type], [num(order.qty, 0), "r"],
      [order.filled_avg_price == null ? "-" : num(order.filled_avg_price), "r"], [order.status],
      [child ? "" : order.by_bot ? "bot" : "manual"],
    ]);
    push(o, false);
    for (const leg of o.legs) push(leg, true);
  }
  fillTable("#orders", rows, 8, "No orders in this period");
}

const KINDS = {
  "signal": ["signal", ""],
  "ai.decision": ["AI", "ai"],
  "ai.invalid_response_final": ["AI invalid", "bad"],
  "ai.all_providers_failed": ["AI down", "bad"],
  "risk.approved": ["approved", "ok"],
  "risk.rejected": ["rejected", "bad"],
  "risk.kill_switch": ["KILL SWITCH", "bad"],
  "risk.force_flatten": ["flatten", "bad"],
  "order.submitted": ["order", "ok"],
  "order.recovered_after_error": ["order", "ok"],
  "symbol.skipped": ["skipped", ""],
};

function describe(e) {
  switch (e.event) {
    case "signal":
      return [`${e.action} · confidence ${num(e.confidence)}` + (e.entry ? ` · entry ${num(e.entry)} SL ${num(e.stop)} TP ${num(e.target)}` : ""), e.reasoning];
    case "ai.decision":
      return [`${e.action} · confidence ${num(e.confidence)} · ${e.model}`, e.reasoning];
    case "risk.rejected":
      return [`${e.action} blocked: ${String(e.reason).replaceAll("_", " ")}`, e.detail];
    case "risk.approved":
      return [`${e.side} ${e.qty} @ ${num(e.entry_price)} · SL ${num(e.stop_loss)} · TP ${num(e.take_profit)} · risk ${money(e.risk_dollars)}`, ""];
    case "order.submitted":
      return [`${e.side} ${e.qty} · ${e.status}` + (e.dry_run ? " (dry run)" : ""), e.client_order_id];
    case "risk.kill_switch":
      return [`Drawdown ${e.drawdown_pct}% · equity ${money(e.equity)} vs start ${money(e.start_equity)}`, ""];
    case "risk.force_flatten":
      return [`Flattening: ${e.reason}`, ""];
    case "symbol.skipped":
      return [`${e.error_type}`, e.error];
    default:
      return [e.error || e.event, e.raw || ""];
  }
}

function renderDecisions() {
  const sym = $("#f-symbol").value, type = $("#f-type").value;
  const list = $("#decisions");
  list.replaceChildren();
  const rows = state.decisions.filter((e) => (!sym || e.symbol === sym) && (!type || e.event === type));
  if (state.tab === "decisions") $("#tab-meta").textContent = `${rows.length} most recent events`;
  if (!rows.length) {
    list.append(el("li", { cls: "muted", text: "No bot decisions recorded yet" }));
    return;
  }
  for (const e of rows) {
    const [kindText, kindCls] = KINDS[e.event] || [e.event, ""];
    const [headline, why] = describe(e);
    const what = el("div", { cls: "what" }, [el("span", { cls: "kind " + kindCls, text: kindText }), headline]);
    if (why) what.append(el("span", { cls: "why", text: why }));
    list.append(el("li", {}, [el("span", { cls: "time", text: when(e.ts) }), el("span", { cls: "sym", text: e.symbol || "-" }), what]));
  }
}

// --- Wiring ------------------------------------------------------------------------
function showError(err) {
  const box = $("#error");
  if (err) { box.textContent = "Some data could not be loaded: " + err.message; box.hidden = false; }
  else box.hidden = true;
}

async function refresh() {
  const jobs = [
    api("api/summary").then(renderSummary),
    api("api/equity?range=" + state.range).then(renderEquity),
    api("api/trades?days=30").then(renderTrades),
    api("api/orders?days=7").then(renderOrders),
    api("api/decisions?limit=300").then((d) => { state.decisions = d.events; renderDecisions(); }),
  ];
  const results = await Promise.allSettled(jobs);
  const failed = results.find((r) => r.status === "rejected");
  showError(failed ? failed.reason : null);
  $("#updated").textContent = "updated " + timeFmt.format(new Date()) + " ET";
}

function selectIn(group, attr, value) {
  for (const b of document.querySelectorAll(`${group} button`)) b.classList.toggle("on", b.dataset[attr] === value);
}

$("#ranges").addEventListener("click", (ev) => {
  const range = ev.target.dataset && ev.target.dataset.range;
  if (!range) return;
  state.range = range;
  selectIn("#ranges", "range", range);
  api("api/equity?range=" + range).then(renderEquity).catch(showError);
});

$("#tabs").addEventListener("click", (ev) => {
  const tab = ev.target.dataset && ev.target.dataset.tab;
  if (!tab) return;
  state.tab = tab;
  selectIn("#tabs", "tab", tab);
  for (const p of document.querySelectorAll("[data-panel]")) p.hidden = p.dataset.panel !== tab;
  $("#tab-meta").textContent = "";
  refresh();
});

$("#f-symbol").addEventListener("change", renderDecisions);
$("#f-type").addEventListener("change", renderDecisions);
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => api("api/equity?range=" + state.range).then(renderEquity).catch(() => {}), 200);
});

refresh();
setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
