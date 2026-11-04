"use strict";
// FX Console. All API text is inserted with textContent / createElement, never innerHTML.

const REFRESH_MS = 15000;
const $ = (sel) => document.querySelector(sel);

const PAGES = {
  overview: ["Overview", "All strategies, accounts and open trades at a glance"],
  strategies: ["Strategies", "Each named strategy trades and is measured on its own"],
  strategy: ["Strategy", "Performance, versions and trades for one strategy"],
  edit: ["New strategy", "Pick a type, pairs, size and exits. Nothing trades until you save."],
  trades: ["Trades", "Every trade from every strategy"],
  activity: ["Activity", "Signals, AI checks, orders, exits and skipped trades, newest first"],
  setup: ["Accounts & AI", "Broker accounts and AI models your strategies can use"],
};

const state = {
  page: "overview", arg: null, meta: null, overview: null, accounts: null, llms: null,
  stFilter: "live", tDays: 30, tStatus: "", tRows: [], events: [], editing: null, llmEditId: null,
};

// ---------- formatting ----------
const fmtDT = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const fmtT = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
const fmtD = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const _ccy = {};
function money(v, ccy = "USD", sign = false) {
  if (v == null || Number.isNaN(v)) return "-";
  let f = _ccy[ccy];
  if (!f) {
    try { f = new Intl.NumberFormat("en-US", { style: "currency", currency: ccy || "USD" }); }
    catch { f = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
    _ccy[ccy] = f;
  }
  return (sign && v > 0 ? "+" : "") + f.format(v);
}
const num = (v, d = 2) => (v == null ? "-" : Number(v).toFixed(d));
const pips = (v) => (v == null ? "-" : (v > 0 ? "+" : "") + Number(v).toFixed(1));
const px = (v) => (v == null ? "-" : String(Number(Number(v).toFixed(6))));
const pct = (v) => (v == null ? "-" : Number(v).toFixed(1) + "%");
const units = (v) => (v == null ? "-" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }));
const when = (iso) => (iso ? fmtDT.format(new Date(iso)) : "-");
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const pairs = (list) => (list || []).map((i) => i.replace("_", "/")).join(", ");
function ago(iso) {
  if (!iso) return "-";
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function duration(seconds) {
  if (seconds == null) return "-";
  const s = Math.round(seconds), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

// ---------- DOM helpers ----------
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.cls) node.className = opts.cls;
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.title) node.title = opts.title;
  if (opts.href) node.href = opts.href;
  if (opts.type) node.type = opts.type;
  if (opts.value != null) node.value = opts.value;
  for (const c of children) if (c != null) node.append(c);
  return node;
}
const tag = (text, cls) => el("span", { cls: "tag " + (cls || String(text).toLowerCase()), text });
function setChip(node, text, cls) { node.textContent = text; node.className = "chip" + (cls ? " " + cls : ""); }

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
    if (row.href) { tr.classList.add("clickable"); tr.addEventListener("click", (e) => { if (!e.target.closest("button")) location.hash = row.href; }); }
    for (const cell of row.cells) {
      const [content, cls] = Array.isArray(cell) ? cell : [cell, ""];
      const td = el("td", { cls });
      td.append(content instanceof Node ? content : document.createTextNode(String(content)));
      tr.append(td);
    }
    tbody.append(tr);
  }
}
function facts(container, pairsList) {
  container.replaceChildren();
  for (const [k, v, cls] of pairsList) container.append(el("dt", { text: k }), el("dd", { text: v, cls }));
}
function statTiles(container, items) {
  container.replaceChildren();
  for (const [label, value, cls, hint] of items) {
    container.append(el("div", { cls: "stat", title: hint }, [el("span", { cls: "k-label", text: label }), el("span", { cls: "s-value " + (cls || ""), text: value })]));
  }
}
let toastTimer;
function toast(text, bad = false) {
  const t = $("#toast");
  t.textContent = text;
  t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = "toast" + (bad ? " bad" : ""); }, bad ? 6000 : 3500);
}
const AVATAR_COLORS = ["#2563eb", "#0e9f6e", "#c2410c", "#7c3aed", "#db2777", "#0891b2", "#4d7c0f", "#b45309"];
function avatar(name, cls = "avatar") {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const a = el("span", { cls, text: name.trim()[0].toUpperCase() });
  a.style.background = AVATAR_COLORS[h % AVATAR_COLORS.length];
  return a;
}
const STATUS = { active: ["Active", "ok"], paused: ["Paused", "warn"], retired: ["Retired", ""] };
const statusChip = (s, halted) => {
  const c = el("span");
  const [t, k] = halted ? ["Halted today", "bad"] : STATUS[s] || [s, ""];
  setChip(c, t, k);
  return c;
};

// ---------- API ----------
async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}
async function post(path, body) {
  const res = await fetch(path, {
    method: "POST", cache: "no-store",
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-FX-Request": "1" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}
async function runCommand(kind, target, pending) {
  toast(pending);
  const { command } = await post("api/commands", { kind, target: String(target) });
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    const c = await api(`api/command?id=${command}`);
    if (c.status === "done" || c.status === "failed") {
      toast(c.result || c.status, c.status === "failed");
      refresh();
      return c;
    }
  }
  toast("The engine has not picked this up yet. Is it running?", true);
  return null;
}
async function meta() {
  if (!state.meta) state.meta = await api("api/meta");
  return state.meta;
}

// ---------- activity text ----------
const REASONS = {
  market_closed: "market closed", not_tradeable: "not tradeable now", spread_too_wide: "spread too wide",
  max_open_trades: "max open trades reached", account_not_hedging: "account can't hold both sides",
  no_currency_conversion: "no currency conversion", size_below_minimum: "size below broker minimum",
  risk_too_large: "loss at stop above hard cap", stop_inside_spread: "stop inside the spread",
  daily_loss_limit: "daily loss limit", ai_rejected: "AI said no", live_trading_disabled: "live trading disabled",
};
const EXITS = {
  stop_loss: "Stop-loss", take_profit: "Take-profit", trailing_stop: "Trailing stop", closed_at_broker: "Closed at broker",
  closed_from_console: "Closed from console", basket_take_profit: "Total profit target", basket_stop_loss: "Total loss limit",
  daily_loss_limit: "Daily loss limit", weekend_close: "Weekend close", margin_closeout: "Margin closeout",
};
function describe(e) {
  const f = e;
  switch (e.event) {
    case "signal": return ["Signal", "", `${f.direction} · ${f.reason}`];
    case "ai.verdict": return ["AI", "ai", `${f.approve ? "approved" : "rejected"} (${num(f.confidence)}) · ${f.model}${f.reason ? " · " + f.reason : ""}`];
    case "risk.rejected": return ["Skipped", "bad", `${REASONS[f.reason] || f.reason}${f.detail ? " · " + f.detail : ""}`];
    case "order.filled": return ["Opened", "ok", `${f.direction} ${units(f.units)} @ ${f.price} · SL ${f.sl}${f.tp ? " · TP " + f.tp : ""} · slippage ${num(f.slippage_pips, 1)} pips`];
    case "order.rejected": return ["Refused", "bad", f.error];
    case "order.unknown": return ["Unclear", "bad", `no reply from broker, checking: ${f.error}`];
    case "order.recovered": return ["Recovered", "ok", "order confirmed at broker after a lost reply"];
    case "order.skipped": return ["Skipped", "bad", f.error];
    case "trade.closed": return ["Closed", f.pnl > 0 ? "ok" : f.pnl < 0 ? "bad" : "", `${EXITS[f.reason] || f.reason} · ${pips(f.pips)} pips · ${money(f.pnl, "USD", true)}`];
    case "trade.close_failed": return ["Close failed", "bad", f.error];
    case "basket.exit": return ["Limit hit", "bad", `${EXITS[f.reason] || f.reason} at ${money(f.open_pl, "USD", true)} open P&L, closing ${f.trades}`];
    case "strategy.loaded": return ["Loaded", "", `v${f.version} · ${f.kind} · ${pairs(f.instruments)} · ${f.granularity} · ${f.status}`];
    case "strategy.invalid": return ["Config error", "bad", f.error];
    case "strategy.error": return ["Error", "bad", f.error];
    case "account.ready": return ["Account", "ok", `${f.account} connected · ${f.currency} · hedging ${f.hedging ? "on" : "off"}`];
    case "account.unavailable": return ["Account", "bad", `${f.account}: ${f.error}`];
    case "command": return ["Console", f.ok ? "" : "bad", `${f.kind.replace("_", " ")} · ${f.result}`];
    case "engine.start": return ["Engine", "ok", "started"];
    case "engine.stop": return ["Engine", "", "stopped · open trades keep their stop-loss / take-profit at the broker"];
    case "data.error": return ["Data", "bad", `${f.granularity}: ${f.error}`];
    default: return [e.event, "", ""];
  }
}
function renderFeed(container, events, compact, emptyText = "Nothing yet.") {
  container.replaceChildren();
  if (!events.length) { container.append(el("li", {}, [el("span", { cls: "empty-li", text: emptyText })])); return; }
  for (const e of events) {
    const [kind, cls, text] = describe(e);
    const who = e.strategy || (e.instrument ? e.instrument.replace("_", "/") : "");
    const what = el("div", { cls: "what" }, [el("span", { cls: "kind " + cls, text: kind })]);
    if (e.instrument && e.strategy) what.append(el("strong", { text: e.instrument.replace("_", "/") + " " }));
    what.append(document.createTextNode(text));
    container.append(el("li", { title: e.ts }, [el("span", { cls: "time", text: compact ? fmtT.format(new Date(e.ts)) : fmtDT.format(new Date(e.ts)) }), el("span", { cls: "s", text: who }), what]));
  }
}

// ---------- charts ----------
const SVG = "http://www.w3.org/2000/svg";
function svg(tagName, attrs = {}) {
  const n = document.createElementNS(SVG, tagName);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}
function sparkline(values) {
  const box = el("div", { cls: "spark" });
  if (!values || values.length < 2) return box;
  const last = values[values.length - 1];
  box.classList.add(last >= 0 ? "up" : "down");
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values), span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${36 - ((v - lo) / span) * 32 - 2}`).join(" ");
  const s = svg("svg", { viewBox: "0 0 100 36", preserveAspectRatio: "none" });
  s.append(svg("polyline", { points: pts, class: "line", "vector-effect": "non-scaling-stroke" }));
  box.append(s);
  return box;
}
function lineChart(container, points, ccy) {
  container.replaceChildren();
  if (points.length < 2) { container.append(el("div", { cls: "empty", text: "The P&L curve appears after the first two closed trades." })); return; }
  const W = container.clientWidth || 600, H = container.clientHeight || 260, L = 64, R = 12, T = 12, B = 26;
  const ys = points.map((p) => p.equity);
  const lo = Math.min(0, ...ys), hi = Math.max(0, ...ys), span = hi - lo || 1;
  const x = (i) => L + (i / (points.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - lo) / span) * (H - T - B);
  const s = svg("svg", { viewBox: `0 0 ${W} ${H}` });
  for (const v of [hi, (hi + lo) / 2, lo]) {
    s.append(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }));
    const t = svg("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end", class: "axis" });
    t.textContent = money(v, ccy);
    s.append(t);
  }
  s.append(svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), class: "zero" }));
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.equity)}`).join("");
  s.append(svg("path", { d: `${d}L${x(points.length - 1)},${y(0)}L${x(0)},${y(0)}Z`, class: "area" }));
  s.append(svg("path", { d, class: "line" }));
  for (const i of [0, points.length - 1]) {
    const t = svg("text", { x: x(i), y: H - 6, "text-anchor": i ? "end" : "start", class: "axis" });
    t.textContent = fmtD.format(new Date(points[i].t));
    s.append(t);
  }
  const cursor = svg("line", { y1: T, y2: H - B, class: "cursor", visibility: "hidden" });
  const dot = svg("circle", { r: 4, class: "dot", visibility: "hidden" });
  s.append(cursor, dot);
  const tip = el("div", { cls: "tip" });
  tip.hidden = true;
  container.append(s, tip);
  s.addEventListener("mousemove", (ev) => {
    const r = s.getBoundingClientRect();
    const px = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(points.length - 1, Math.round(((px - L) / (W - L - R)) * (points.length - 1))));
    const p = points[i];
    for (const n of [cursor, dot]) n.setAttribute("visibility", "visible");
    cursor.setAttribute("x1", x(i)); cursor.setAttribute("x2", x(i));
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.equity));
    tip.hidden = false;
    tip.textContent = `${when(p.t)} · ${money(p.equity, ccy, true)} (trade ${i + 1})`;
    tip.style.left = Math.min(r.width - 220, Math.max(0, (x(i) / W) * r.width + 10)) + "px";
  });
  s.addEventListener("mouseleave", () => { tip.hidden = true; for (const n of [cursor, dot]) n.setAttribute("visibility", "hidden"); });
}

// ---------- top bar ----------
const SESSIONS = {
  open: ["Market open", "ok"], closing: ["Friday close · no new trades", "warn"],
  weekend_close: ["Closing for the weekend", "warn"], closed: ["Market closed · opens Sun 17:05 NY", ""],
};
function renderTop(o) {
  const [st, sc] = SESSIONS[o.session] || [o.session, ""];
  setChip($("#session"), st, sc);
  const e = o.engine;
  const map = {
    running: [`Engine running · ${duration(e.uptime_s)}`, "ok", "Engine running", "ok"],
    not_reporting: [`Engine silent ${ago(e.ts)}`, "bad", "Engine not reporting", "bad"],
    stopped: ["Engine stopped", "bad", "Engine stopped", "bad"],
    never_started: ["Engine not started", "warn", "Engine not started", "bad"],
  };
  const [t, c, side, dot] = map[e.status] || [e.status, "", e.status, ""];
  setChip($("#engine"), t, c);
  $("#side-engine").textContent = side;
  $("#side-dot").className = "dot " + dot;
  const banner = $("#banner");
  if (e.status === "not_reporting" || e.status === "stopped") {
    banner.textContent = "The engine is not running, so no strategy is trading. Open trades keep their broker-side stop-loss and take-profit. On the server: sudo systemctl status fx-engine";
    banner.hidden = false;
  } else banner.hidden = true;
  $("#updated").textContent = "Updated " + fmtT.format(new Date());
}

// ---------- overview ----------
function mainCurrency(totals) {
  const entries = Object.entries(totals || {});
  entries.sort((a, b) => b[1].balance - a[1].balance);
  return entries.length ? entries[0] : ["USD", { balance: null, nav: null, unrealized: null, today: null }];
}
function renderOverview(o) {
  const c = o.counts;
  const onboard = $("#onboarding");
  if (!c.accounts || !c.strategies) {
    const steps = $("#onboard-steps");
    steps.replaceChildren(
      el("li", { cls: c.accounts ? "done" : "", text: "Add your OANDA practice account (Accounts & AI)" }),
      el("li", { cls: c.llms ? "done" : "", text: "Optional: add an AI model if strategies should ask AI to confirm" }),
      el("li", { cls: c.strategies ? "done" : "", text: "Create a strategy: give it a name, pairs, size and exits" }));
    const next = $("#onboard-next");
    next.href = c.accounts ? "#new" : "#setup";
    next.textContent = c.accounts ? "Create your first strategy" : "Add an account";
    onboard.hidden = false;
  } else onboard.hidden = true;

  const [ccy, t] = mainCurrency(o.totals);
  $("#k-balance").textContent = money(t.balance, ccy);
  const others = Object.keys(o.totals).filter((k) => k !== ccy);
  $("#k-balance-sub").textContent = t.nav != null ? `NAV ${money(t.nav, ccy)}${others.length ? " · +" + others.join(", ") : ""}` : "no account data yet";
  const today = $("#k-today");
  today.textContent = money(t.today, ccy, true);
  today.className = "k-value " + tone(t.today);
  $("#k-today-sub").textContent = `${c.trades_today} closed since 17:00 NY`;
  const open = $("#k-open");
  open.textContent = c.open_trades ? money(t.unrealized, ccy, true) : "-";
  open.className = "k-value " + tone(t.unrealized);
  $("#k-open-sub").textContent = c.open_trades ? `${c.open_trades} open trade${c.open_trades > 1 ? "s" : ""}` : "No open trades";
  $("#k-strats").textContent = `${c.active} active`;
  $("#k-strats-sub").textContent = `${c.strategies} total · ${c.accounts} account${c.accounts === 1 ? "" : "s"}`;
  const best = o.leaderboard.find((s) => s.stats.trades);
  $("#k-best").textContent = best ? best.name : "-";
  $("#k-best-sub").textContent = best ? `${money(best.pnl_30d, best.currency, true)} · ${pct(best.stats.win_rate)} win rate` : "no closed trades yet";

  fillTable("#leaderboard", o.leaderboard.map((s) => ({
    href: `#strategy/${encodeURIComponent(s.name)}`,
    cells: [
      el("span", { cls: "sym", text: s.name }), kindTitle(s.kind), pairs(s.instruments), statusChip(s.status, s.halted),
      [s.stats.trades, "r"], [pct(s.stats.win_rate), "r"], [num(s.stats.profit_factor), "r"],
      [money(s.pnl_today, s.currency, true), "r " + tone(s.pnl_today)], [money(s.pnl_30d, s.currency, true), "r " + tone(s.pnl_30d)],
    ],
  })), "No strategies yet.");

  $("#open-meta").textContent = o.open_trades.length ? `${o.open_trades.length} open` : "";
  fillTable("#open-trades", o.open_trades.map((t) => ({
    cells: [
      el("a", { href: `#strategy/${encodeURIComponent(t.strategy)}`, text: t.strategy }), el("span", { cls: "sym", text: t.instrument.replace("_", "/") }),
      tag(t.direction), [units(t.units), "r"], [px(t.entry_price), "r"], [px(t.sl_price), "r"], [px(t.tp_price), "r"],
      when(t.opened_at), closeButton(t),
    ],
  })), "No open trades.");
  renderFeed($("#recent"), o.recent, true);
}
function closeButton(t) {
  const b = el("button", { cls: "btn small danger", type: "button", text: "Close" });
  b.addEventListener("click", async () => {
    if (!confirm(`Close ${t.strategy}'s ${t.direction} ${t.instrument.replace("_", "/")} trade at market now?`)) return;
    b.disabled = true;
    try { await runCommand("close_trade", t.id, "Asking the engine to close the trade..."); } catch (e) { toast(e.message, true); b.disabled = false; }
  });
  return b;
}
function kindTitle(kind) {
  const k = (state.meta?.catalog || []).find((c) => c.kind === kind);
  return k ? k.title : kind;
}

// ---------- strategies ----------
function renderCards(list) {
  const cards = $("#cards");
  cards.replaceChildren();
  const f = state.stFilter;
  const shown = list.filter((s) => (f === "all" ? true : f === "retired" ? s.status === "retired" : s.status !== "retired"));
  if (!shown.length) {
    const empty = el("div", { cls: "card empty-card" }, [
      el("h2", { text: f === "retired" ? "No retired strategies" : "No strategies yet" }),
      el("p", { cls: "sub", text: "A strategy is a named set of rules with its own pairs, size, exits and (optionally) AI model." }),
    ]);
    if (f !== "retired") empty.append(el("a", { cls: "btn primary", href: "#new", text: "+ New strategy" }));
    cards.append(empty);
    return;
  }
  for (const s of shown) {
    const st = s.stats;
    const meta = el("div", { cls: "s-meta" }, [
      el("span", { cls: "pill", text: s.granularity }), el("span", { cls: "pill", text: pairs(s.instruments) }),
      el("span", { cls: "pill", text: s.account || "account ?" }),
    ]);
    if (s.ai_enabled) meta.append(el("span", { cls: "pill ai", text: "AI · " + (s.ai_model || "default") }));
    if (s.open_trades) meta.append(el("span", { cls: "pill", text: `${s.open_trades} open` }));
    if (s.error) meta.append(el("span", { cls: "pill", text: "config error", title: s.error }));
    const card = el("div", { cls: "card s-card" + (s.status === "retired" ? " retired" : "") }, [
      el("div", { cls: "s-top" }, [avatar(s.name), el("div", {}, [el("div", { cls: "s-name", text: s.name }), el("div", { cls: "s-kind", text: `${kindTitle(s.kind)} · v${s.version}` })]), statusChip(s.status, s.halted)]),
      el("div", { cls: "s-nums" }, [
        el("div", {}, [el("span", { cls: "k-label", text: "Net P&L" }), el("strong", { cls: tone(st.net_pnl), text: money(st.net_pnl, s.currency, true) })]),
        el("div", {}, [el("span", { cls: "k-label", text: "Win rate" }), el("strong", { text: pct(st.win_rate) })]),
        el("div", {}, [el("span", { cls: "k-label", text: "Trades" }), el("strong", { text: st.trades })]),
        el("div", {}, [el("span", { cls: "k-label", text: "Pips" }), el("strong", { cls: tone(st.net_pips), text: pips(st.net_pips) })]),
      ]),
      sparkline(s.spark), meta,
    ]);
    card.addEventListener("click", () => { location.hash = `#strategy/${encodeURIComponent(s.name)}`; });
    cards.append(card);
  }
}

// ---------- strategy detail ----------
function actionButton(text, cls, fn) {
  const b = el("button", { cls: "btn " + cls, type: "button", text });
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await fn(); } catch (e) { toast(e.message, true); } finally { b.disabled = false; }
  });
  return b;
}
async function setStatus(name, status) {
  await post("api/strategies/status", { name, status });
  toast(`${name} is now ${status}.`);
  refresh();
}
function renderDetail(d) {
  const s = d.strategy, st = s.stats, ccy = s.currency || "USD";
  PAGES.strategy[0] = s.name;
  $("#page-title").textContent = s.name;
  $("#d-avatar").replaceWith(Object.assign(avatar(s.name, "avatar lg"), { id: "d-avatar" }));
  $("#d-name").textContent = s.name;
  $("#d-sub").textContent = `${kindTitle(s.kind)} · ${pairs(s.instruments)} · ${s.granularity} · ${s.account}${s.notes ? " · " + s.notes : ""}`;
  $("#d-status").replaceWith(Object.assign(statusChip(s.status, s.halted), { id: "d-status" }));

  const actions = $("#d-actions");
  actions.replaceChildren();
  if (s.status !== "retired") {
    actions.append(el("a", { cls: "btn", href: `#edit/${encodeURIComponent(s.name)}`, text: "Edit" }));
    actions.append(s.status === "active"
      ? actionButton("Pause", "", () => setStatus(s.name, "paused"))
      : actionButton("Resume", "primary", () => setStatus(s.name, "active")));
  }
  actions.append(actionButton("Duplicate", "", async () => {
    const name = prompt(`Name for the copy of ${s.name} (a new person's name):`);
    if (!name) return;
    const r = await post("api/strategies/duplicate", { name: s.name, new_name: name });
    toast(`${r.strategy.name} created (paused). Adjust it, then resume.`);
    location.hash = `#edit/${encodeURIComponent(r.strategy.name)}`;
  }));
  if (s.open_trades) {
    actions.append(actionButton(`Close ${s.open_trades} trade${s.open_trades > 1 ? "s" : ""}`, "danger", async () => {
      if (confirm(`Close all of ${s.name}'s open trades at market now?`)) await runCommand("close_strategy", s.name, "Closing trades...");
    }));
  }
  if (s.status !== "retired") {
    actions.append(actionButton("Retire", "danger", async () => {
      if (confirm(`Retire ${s.name}? It stops for good and the name stays reserved. History is kept.`)) await setStatus(s.name, "retired");
    }));
  }

  statTiles($("#d-stats"), [
    ["Net P&L", money(st.net_pnl, ccy, true), tone(st.net_pnl)], ["Net pips", pips(st.net_pips), tone(st.net_pips)],
    ["Trades", st.trades], ["Win rate", pct(st.win_rate)], ["Profit factor", num(st.profit_factor), "", "gross profit / gross loss"],
    ["Expectancy", money(st.expectancy, ccy, true), tone(st.expectancy), "average P&L per trade"],
    ["Avg win", money(st.avg_win, ccy), "up"], ["Avg loss", money(st.avg_loss, ccy), "down"],
    ["Avg hold", st.avg_hold_min == null ? "-" : duration(st.avg_hold_min * 60)], ["Today", money(s.pnl_today, ccy, true), tone(s.pnl_today)],
  ]);
  $("#d-ccy").textContent = ccy;
  lineChart($("#d-chart"), d.curve, ccy);

  const r = s.risk, llmText = s.ai_enabled ? `${s.ai_label || "default"} (${s.ai_model || "?"}), min ${r.ai_min_confidence}` : "off";
  $("#d-version").textContent = `version ${s.version}`;
  facts($("#d-config"), [
    ["Size", r.sizing === "units" ? `${units(r.units)} units` : `${r.risk_pct}% of balance at risk`],
    ["Stop-loss", r.sl_pips != null ? `${r.sl_pips} pips` : money(r.sl_amount, ccy)],
    ["Take-profit", r.tp_pips != null ? `${r.tp_pips} pips` : r.tp_amount != null ? money(r.tp_amount, ccy) : "none"],
    ["Total profit / loss exit", `${r.basket_tp_amount ? money(r.basket_tp_amount, ccy) : "off"} / ${r.basket_sl_amount ? money(r.basket_sl_amount, ccy) : "off"}`],
    ["Daily loss limit", r.daily_loss_limit ? money(r.daily_loss_limit, ccy) : "off"],
    ["Max open trades", r.max_open_trades], ["Max spread / slippage", `${r.max_spread_pips} / ${r.max_slippage_pips} pips`],
    ["Weekend", r.close_before_weekend ? "closes Friday 16:50 NY" : "holds over weekend"], ["AI", llmText],
    ["Signal settings", Object.entries(s.params).filter(([, v]) => v != null).map(([k, v]) => `${k}=${v}`).join(", ") || "defaults"],
    ["Last candle", s.last_bar ? when(s.last_bar) : "-"],
  ]);

  fillTable("#d-versions", d.versions.map((v) => ({
    cls: v.version === s.version ? "current" : "",
    cells: [`v${v.version}${v.version === s.version ? " (current)" : ""}`, when(v.changed_at), v.note || "-", [v.stats.trades, "r"],
      [pct(v.stats.win_rate), "r"], [num(v.stats.profit_factor), "r"], [pips(v.stats.net_pips), "r " + tone(v.stats.net_pips)],
      [money(v.stats.net_pnl, ccy, true), "r " + tone(v.stats.net_pnl)]],
  })), "No versions.");
  fillTable("#d-pairs", d.by_instrument.map((p) => ({
    cells: [el("span", { cls: "sym", text: p.instrument.replace("_", "/") }), [p.trades, "r"], [pct(p.win_rate), "r"],
      [pips(p.pips), "r " + tone(p.pips)], [money(p.net_pnl, ccy, true), "r " + tone(p.net_pnl)]],
  })), "No closed trades yet.");
  renderFeed($("#d-events"), d.events.slice(0, 25), true);
  $("#d-trades-meta").textContent = `${d.trades.length} shown`;
  fillTable("#d-trades", d.trades.map((t) => ({
    cells: [when(t.closed_at || t.opened_at || t.created_at), el("span", { cls: "sym", text: t.instrument.replace("_", "/") }), tag(t.direction),
      [units(t.units), "r"], [px(t.entry_price), "r"], [px(t.exit_price), "r"], [pips(t.pips), "r " + tone(t.pips)],
      [money(t.pnl, ccy, true), "r " + tone(t.pnl)], t.status === "closed" ? (EXITS[t.close_reason] || t.close_reason || "-") : t.status === "failed" ? (t.error || "failed") : t.status,
      `v${t.strategy_version}`, t.status === "open" ? closeButton(t) : ""],
  })), "No trades yet.");
}

// ---------- editor ----------
function humanize(key) { return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()); }
function propType(p) {
  if (p.anyOf) {
    const t = p.anyOf.find((x) => x.type !== "null");
    return { ...p, ...t, nullable: true };
  }
  return p;
}
function renderParams(kind, values = {}) {
  const box = $("#s-params");
  box.replaceChildren();
  const k = state.meta.catalog.find((c) => c.kind === kind);
  $("#s-kind-desc").textContent = k ? k.description : "";
  if (!k) return;
  for (const [key, raw] of Object.entries(k.params.properties || {})) {
    const p = propType(raw);
    const value = key in values ? values[key] : p.default;
    let input;
    if (p.enum) {
      input = el("select");
      for (const v of p.enum) input.append(el("option", { value: v, text: humanize(String(v)) }));
      input.value = value ?? p.enum[0];
    } else if (p.type === "boolean") {
      input = el("input", { type: "checkbox" });
      input.checked = !!value;
    } else {
      input = el("input", { type: "number" });
      input.step = p.type === "integer" ? "1" : "any";
      if (p.minimum != null) input.min = p.minimum;
      if (p.maximum != null) input.max = p.maximum;
      input.value = value ?? "";
      if (p.nullable) input.placeholder = "off";
    }
    input.dataset.key = key;
    input.dataset.kind = p.enum ? "enum" : p.type;
    input.dataset.nullable = p.nullable ? "1" : "";
    const label = el("label", { cls: p.type === "boolean" ? "check" : "f" });
    if (p.type === "boolean") label.append(input, document.createTextNode(" " + (p.title || humanize(key))));
    else label.append(document.createTextNode(p.title || humanize(key)), input);
    if (p.description && p.type !== "boolean") label.append(el("span", { cls: "hint", text: p.description }));
    box.append(label);
  }
}
function collectParams() {
  const out = {};
  for (const input of $("#s-params").querySelectorAll("[data-key]")) {
    const key = input.dataset.key;
    if (input.dataset.kind === "boolean") out[key] = input.checked;
    else if (input.dataset.kind === "enum") out[key] = input.value;
    else if (input.value === "") { if (input.dataset.nullable) out[key] = null; }
    else out[key] = Number(input.value);
  }
  return out;
}
const RISK_NUM = ["units", "risk_pct", "basket_tp_amount", "basket_sl_amount", "daily_loss_limit", "max_open_trades", "max_spread_pips", "max_slippage_pips", "ai_min_confidence"];
function fillRisk(r) {
  $("#r-sizing").value = r.sizing;
  for (const k of RISK_NUM) $("#r-" + k).value = r[k] ?? "";
  $("#r-sl-mode").value = r.sl_amount != null ? "amount" : "pips";
  $("#r-sl").value = r.sl_amount ?? r.sl_pips ?? "";
  $("#r-tp-mode").value = r.tp_amount != null ? "amount" : r.tp_pips != null ? "pips" : "none";
  $("#r-tp").value = r.tp_amount ?? r.tp_pips ?? "";
  $("#r-close_before_weekend").checked = !!r.close_before_weekend;
  syncEditor();
}
function collectRisk() {
  const n = (id) => ($(id).value === "" ? null : Number($(id).value));
  const r = { sizing: $("#r-sizing").value, close_before_weekend: $("#r-close_before_weekend").checked };
  for (const k of RISK_NUM) { const v = n("#r-" + k); if (v != null || k.includes("amount") || k === "daily_loss_limit") r[k] = v; }
  const sl = n("#r-sl"), tp = n("#r-tp");
  const slMode = $("#r-sl-mode").value, tpMode = $("#r-tp-mode").value;
  r.sl_pips = slMode === "pips" ? sl : null;
  r.sl_amount = slMode === "amount" ? sl : null;
  r.tp_pips = tpMode === "pips" ? tp : null;
  r.tp_amount = tpMode === "amount" ? tp : null;
  return r;
}
function syncEditor() {
  const risk = $("#r-sizing").value === "risk_pct";
  $("#w-units").hidden = risk;
  $("#w-risk").hidden = !risk;
  if (risk && $("#r-sl-mode").value === "amount") $("#r-sl-mode").value = "pips";
  $("#r-sl-mode").querySelector('[value="amount"]').disabled = risk;
  $("#r-tp").disabled = $("#r-tp-mode").value === "none";
  const ai = $("#s-ai").checked;
  $("#w-llm").hidden = !ai;
  $("#w-conf").hidden = !ai;
  $("#h-risk").textContent = `Hard limit ${state.meta.hard_limits.max_risk_pct}%. Needs a stop-loss in pips.`;
}
async function openEditor(name) {
  await meta();
  const [accounts, llms] = await Promise.all([api("api/accounts"), api("api/llms")]);
  state.accounts = accounts; state.llms = llms;
  let rec = null;
  if (name) rec = (await api(`api/strategy?name=${encodeURIComponent(name)}`)).strategy;
  state.editing = rec;
  $("#page-title").textContent = rec ? `Edit ${rec.name}` : "New strategy";
  $("#page-sub").textContent = rec ? "Saving a change to trading settings starts a new version; history stays comparable." : PAGES.edit[1];
  $("#edit-back").href = $("#s-cancel").href = rec ? `#strategy/${encodeURIComponent(rec.name)}` : "#strategies";
  $("#s-submit").textContent = rec ? "Save changes" : "Create strategy";
  $("#w-change").hidden = !rec;
  $("#s-error").textContent = "";

  const kind = $("#s-kind");
  kind.replaceChildren(...state.meta.catalog.map((c) => el("option", { value: c.kind, text: c.title })));
  const acc = $("#s-account");
  acc.replaceChildren(...accounts.accounts.map((a) => el("option", { value: a.id, text: `${a.label} · ${a.environment}${a.currency ? " · " + a.currency : ""}` })));
  if (!accounts.accounts.length) acc.append(el("option", { value: "", text: "Add an account first (Accounts & AI)" }));
  const gran = $("#s-granularity");
  const GN = { M1: "1 minute", M5: "5 minutes", M15: "15 minutes", M30: "30 minutes", H1: "1 hour", H4: "4 hours" };
  gran.replaceChildren(...state.meta.granularities.map((g) => el("option", { value: g, text: GN[g] || g })));
  const def = llms.profiles.find((p) => p.is_default);
  const llm = $("#s-llm");
  llm.replaceChildren(el("option", { value: "", text: def ? `Default · ${def.label}` : "Default (none set yet)" }),
    ...llms.profiles.map((p) => el("option", { value: p.id, text: `${p.label} · ${p.model}` })));

  $("#s-name").value = rec ? rec.name : "";
  $("#s-name").readOnly = !!rec;
  kind.value = rec ? rec.kind : state.meta.catalog[0].kind;
  acc.value = rec ? rec.account_id : (accounts.accounts[0]?.id ?? "");
  $("#s-instruments").value = rec ? rec.instruments.join(", ") : "EUR_USD";
  gran.value = rec ? rec.granularity : "M5";
  $("#s-ai").checked = rec ? rec.ai_enabled : false;
  llm.value = rec?.llm_profile_id ?? "";
  $("#s-notes").value = rec ? rec.notes : "";
  $("#s-change").value = "";
  renderParams(kind.value, rec ? rec.params : {});
  fillRisk(rec ? rec.risk : state.meta.risk_defaults);
}
async function submitStrategy(ev) {
  ev.preventDefault();
  const err = $("#s-error");
  err.textContent = "";
  const rec = state.editing;
  const body = {
    name: $("#s-name").value.trim(), kind: $("#s-kind").value, account_id: Number($("#s-account").value),
    instruments: $("#s-instruments").value, granularity: $("#s-granularity").value, params: collectParams(),
    risk: collectRisk(), ai_enabled: $("#s-ai").checked, llm_profile_id: $("#s-llm").value ? Number($("#s-llm").value) : null,
    notes: $("#s-notes").value.trim(),
  };
  if (!body.name) { err.textContent = "Give the strategy a name."; return; }
  if (!body.account_id) { err.textContent = "Add an OANDA account first (Accounts & AI)."; return; }
  if (rec) body.change_note = $("#s-change").value.trim();
  const btn = $("#s-submit");
  btn.disabled = true;
  try {
    const r = await post(rec ? "api/strategies/update" : "api/strategies", body);
    toast(rec ? `${r.strategy.name} saved (v${r.strategy.version}).` : `${r.strategy.name} created and active. The engine picks it up within seconds.`);
    location.hash = `#strategy/${encodeURIComponent(r.strategy.name)}`;
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

// ---------- trades ----------
function renderTrades(data) {
  state.tRows = data.trades;
  const st = data.stats;
  statTiles($("#t-stats"), [
    ["Closed trades", st.trades], ["Win rate", pct(st.win_rate)], ["Net P&L", money(st.net_pnl, "USD", true), tone(st.net_pnl)],
    ["Net pips", pips(st.net_pips), tone(st.net_pips)], ["Profit factor", num(st.profit_factor)], ["Best", money(st.best, "USD", true), "up"], ["Worst", money(st.worst, "USD", true), "down"],
  ]);
  fillTable("#t-table", data.trades.map((t) => ({
    cells: [when(t.closed_at || t.opened_at || t.created_at), el("a", { href: `#strategy/${encodeURIComponent(t.strategy)}`, text: t.strategy }),
      el("span", { cls: "sym", text: t.instrument.replace("_", "/") }), tag(t.direction), [units(t.units), "r"], [px(t.entry_price), "r"],
      [px(t.exit_price), "r"], [pips(t.pips), "r " + tone(t.pips)], [money(t.pnl, "USD", true), "r " + tone(t.pnl)], tag(t.status, t.status === "closed" ? "muted" : t.status),
      t.status === "closed" ? (EXITS[t.close_reason] || t.close_reason || "-") : t.status === "failed" ? (t.error || "-") : t.signal_reason || "-"],
  })), "No trades in this period.");
}
function exportTrades() {
  const cols = ["id", "strategy", "strategy_version", "account", "instrument", "direction", "units", "status", "entry_price", "exit_price", "sl_price", "tp_price", "pips", "pnl", "opened_at", "closed_at", "close_reason", "signal_reason", "ai_model"];
  const esc = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  const csv = [cols.join(","), ...state.tRows.map((t) => cols.map((c) => esc(t[c])).join(","))].join("\n");
  const a = el("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })) });
  a.download = `fx-trades-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------- setup ----------
function renderSetup(accounts, llms) {
  const warn = $("#setup-warn");
  const notes = [];
  if (!accounts.can_store_secrets) notes.push("FX_MASTER_KEY is not set on the server, so accounts and AI keys cannot be saved. Run: python -m fx keygen");
  if (!accounts.allow_live) notes.push("Live accounts can be added, but the engine will not trade them until FX_ALLOW_LIVE=true is set on the server.");
  warn.replaceChildren(...notes.map((n) => el("div", { text: n })));
  warn.hidden = !notes.length;

  fillTable("#accounts", accounts.accounts.map((a) => {
    const s = a.summary || {};
    const ok = a.connected && !a.engine_error;
    const status = el("span");
    setChip(status, ok ? (a.stream === "connected" ? "Connected" : "Connected · stream " + (a.stream || "?")) : (a.engine_error || a.last_error ? "Error" : "Waiting for engine"), ok ? "ok" : a.engine_error || a.last_error ? "bad" : "warn");
    status.title = a.engine_error || a.last_error || "";
    const test = el("button", { cls: "btn small", type: "button", text: "Test" });
    test.addEventListener("click", () => runCommand("check_account", a.id, `Testing ${a.label}...`).catch((e) => toast(e.message, true)));
    return {
      cells: [el("span", { cls: "sym", text: a.label }), tag(a.environment, a.environment === "live" ? "short" : "muted"), el("span", { cls: "mono", text: a.oanda_account_id }),
        el("span", { cls: "mono", text: a.token_hint }), a.currency || "-", [s.balance != null ? money(s.balance, a.currency) : "-", "r"],
        a.hedging == null ? "-" : a.hedging ? "on" : "off (one side per pair)", status, test],
    };
  }), "No accounts yet. Add your OANDA practice account below.");

  fillTable("#llms", llms.profiles.map((p) => {
    const actions = el("span", { cls: "actions" });
    const edit = el("button", { cls: "btn small", type: "button", text: "Edit" });
    edit.addEventListener("click", () => {
      state.llmEditId = p.id;
      $("#llm-label").value = p.label; $("#llm-label").readOnly = true;
      $("#llm-model").value = p.model; $("#llm-base").value = p.api_base || ""; $("#llm-key").value = "";
      $("#llm-key").placeholder = p.api_key_hint ? `keep ${p.api_key_hint}` : "optional";
      $("#llm-default").checked = p.is_default;
      $("#llm-submit").textContent = "Save AI model"; $("#llm-cancel").hidden = false; $("#llm-title").textContent = `Edit ${p.label}`;
      $("#llm-model").focus();
    });
    actions.append(edit);
    if (!p.is_default) {
      const def = el("button", { cls: "btn small", type: "button", text: "Make default" });
      def.addEventListener("click", async () => { try { await post("api/llms/default", { id: p.id }); toast(`${p.label} is now the default.`); loadPage(); } catch (e) { toast(e.message, true); } });
      actions.append(def);
    }
    return { cells: [el("span", { cls: "sym", text: p.label }), el("span", { cls: "mono", text: p.model }), p.api_key_hint || "from server .env", p.api_base || "-", p.is_default ? tag("default", "filled") : "", actions] };
  }), "No AI models yet. Only needed for strategies with AI confirmation.");
}
function resetLlmForm() {
  state.llmEditId = null;
  $("#llm-form").reset();
  $("#llm-label").readOnly = false;
  $("#llm-key").placeholder = "optional";
  $("#llm-submit").textContent = "Add AI model";
  $("#llm-title").textContent = "Add an AI model";
  $("#llm-cancel").hidden = true;
  $("#llm-error").textContent = "";
}

// ---------- routing & loading ----------
const LOADERS = {
  async overview() { renderOverview(state.overview); },
  async strategies() { await meta(); renderCards((await api("api/strategies")).strategies); },
  async strategy() { await meta(); renderDetail(await api(`api/strategy?name=${encodeURIComponent(state.arg)}`)); },
  async edit() { /* loaded once on navigation, never auto-refreshed (would wipe the form) */ },
  async trades() {
    const sel = $("#t-strategy");
    if (sel.options.length === 1) for (const s of state.overview.leaderboard) sel.append(el("option", { value: s.name, text: s.name }));
    renderTrades(await api(`api/trades?days=${state.tDays}&status=${state.tStatus}&strategy=${encodeURIComponent(sel.value)}`));
  },
  async activity() {
    const sel = $("#a-strategy");
    if (sel.options.length === 1) for (const s of state.overview.leaderboard) sel.append(el("option", { value: s.name, text: s.name }));
    state.events = (await api(`api/events?limit=500&strategy=${encodeURIComponent(sel.value)}`)).events;
    renderActivity();
  },
  async setup() {
    const [a, l] = await Promise.all([api("api/accounts"), api("api/llms")]);
    renderSetup(a, l);
  },
};
function renderActivity() {
  const q = $("#a-search").value.trim().toLowerCase();
  const rows = q ? state.events.filter((e) => JSON.stringify(e).toLowerCase().includes(q)) : state.events;
  renderFeed($("#a-feed"), rows, false, "No activity matches.");
}
function parseHash() {
  const h = decodeURIComponent(location.hash.slice(1) || "overview");
  if (h === "new") return ["edit", null];
  const [page, ...rest] = h.split("/");
  if (page === "edit") return ["edit", rest.join("/") || null];
  if (page === "strategy") return ["strategy", rest.join("/")];
  return [PAGES[page] ? page : "overview", null];
}
async function loadPage() {
  try {
    state.overview = await api("api/overview");
    renderTop(state.overview);
    await meta();
    await LOADERS[state.page]();
  } catch (e) {
    toast(e.message, true);
  }
}
async function navigate() {
  const [page, arg] = parseHash();
  state.page = page; state.arg = arg;
  for (const s of document.querySelectorAll("[data-page]")) s.hidden = s.dataset.page !== page;
  const navKey = page === "strategy" || page === "edit" ? "strategies" : page;
  for (const a of document.querySelectorAll("[data-nav]")) a.classList.toggle("active", a.dataset.nav === navKey);
  $("#page-title").textContent = PAGES[page][0];
  $("#page-sub").textContent = PAGES[page][1];
  window.scrollTo(0, 0);
  await loadPage();
  if (page === "edit") { try { await openEditor(arg); } catch (e) { toast(e.message, true); } }
}
async function refresh() {
  const btn = $("#refresh");
  btn.classList.add("spin");
  await loadPage();
  btn.classList.remove("spin");
}

function seg(id, key, after) {
  $(id).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    for (const x of $(id).children) x.classList.toggle("on", x === b);
    state[key] = isNaN(Number(b.dataset.v)) || b.dataset.v === "" ? b.dataset.v : Number(b.dataset.v);
    after();
  });
}

function init() {
  try {
    const saved = localStorage.getItem("fx-theme");
    if (saved) document.documentElement.dataset.theme = saved;
  } catch { /* storage unavailable */ }
  $("#theme").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("fx-theme", document.documentElement.dataset.theme); } catch { /* ignore */ }
  });
  $("#refresh").addEventListener("click", refresh);
  seg("#st-filter", "stFilter", () => LOADERS.strategies());
  seg("#t-status", "tStatus", () => LOADERS.trades());
  seg("#t-days", "tDays", () => LOADERS.trades());
  $("#t-strategy").addEventListener("change", () => LOADERS.trades());
  $("#t-export").addEventListener("click", exportTrades);
  $("#a-strategy").addEventListener("change", () => LOADERS.activity());
  $("#a-search").addEventListener("input", renderActivity);

  $("#s-form").addEventListener("submit", submitStrategy);
  $("#s-kind").addEventListener("change", (e) => renderParams(e.target.value, {}));
  for (const id of ["#r-sizing", "#r-sl-mode", "#r-tp-mode", "#s-ai"]) $(id).addEventListener("change", syncEditor);

  $("#acc-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("#acc-error").textContent = "";
    try {
      const r = await post("api/accounts", { label: $("#acc-label").value.trim(), environment: $("#acc-env").value, oanda_account_id: $("#acc-id").value.trim(), token: $("#acc-token").value.trim() });
      $("#acc-form").reset();
      toast("Account saved. The engine is testing the connection...");
      await LOADERS.setup();
      runCommand("check_account", r.id, "Testing the connection...").catch(() => {});
    } catch (e) { $("#acc-error").textContent = e.message; }
  });
  $("#llm-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("#llm-error").textContent = "";
    try {
      const body = { label: $("#llm-label").value.trim(), model: $("#llm-model").value.trim(), api_key: $("#llm-key").value.trim(), api_base: $("#llm-base").value.trim(), is_default: $("#llm-default").checked };
      if (state.llmEditId) body.id = state.llmEditId;
      await post("api/llms", body);
      toast(state.llmEditId ? "AI model saved." : "AI model added.");
      resetLlmForm();
      await LOADERS.setup();
    } catch (e) { $("#llm-error").textContent = e.message; }
  });
  $("#llm-cancel").addEventListener("click", resetLlmForm);

  window.addEventListener("hashchange", navigate);
  navigate();
  setInterval(() => {
    if (document.hidden || state.page === "edit" || (state.page === "setup" && document.activeElement?.closest("form"))) return;
    loadPage();
  }, REFRESH_MS);
}
init();
