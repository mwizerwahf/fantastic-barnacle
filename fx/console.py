"""Forex console: onboarding forms + per-strategy performance, served on 127.0.0.1 behind nginx.

The console never talks to OANDA and never decrypts anything. It reads SQLite and the engine's
heartbeat, encrypts secrets on the way in, and asks the engine to act (close a trade, test an
account) through the ``commands`` table.

Writes are JSON POSTs that must carry ``X-FX-Request: 1`` and a same-origin ``Origin``: a page on
another site cannot produce that request, so browser-cached passwords can't be abused (CSRF).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.observability import log_event, setup_logging
from dashboard.history import trade_stats
from fx import risk as fx_risk
from fx.config import FxSettings
from fx.crypto import SecretBox, hint
from fx.db import FxStore, StoreError
from fx.models import GRANULARITY_SECONDS, RiskConfig, StrategyRecord, StrategyStatus, validate_name
from fx.strategies import catalog, validate_params

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {"index.html", "app.js", "style.css"}
MAX_BODY = 64 * 1024
ENGINE_STALE_S = 60
OANDA_ACCOUNT_RE = re.compile(r"^\d{3}-\d{3}-\d{4,12}-\d{3}$")


# ------------------------------------------------------------------ input models
class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=40)
    environment: Literal["practice", "live"]
    oanda_account_id: str
    token: str = Field(min_length=20, max_length=200)

    @field_validator("oanda_account_id")
    @classmethod
    def _acct(cls, v: str) -> str:
        if not OANDA_ACCOUNT_RE.match(v):
            raise ValueError("OANDA account IDs look like 101-004-1234567-001")
        return v


class LlmIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: int | None = None
    label: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=3, max_length=120, pattern=r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.:@/\-]+$")
    api_key: str | None = Field(default=None, max_length=400)
    api_base: str | None = Field(default=None, max_length=200, pattern=r"^https?://[^\s]+$")
    is_default: bool = False

    @field_validator("api_key", "api_base", mode="before")
    @classmethod
    def _blank(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v


class StrategyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str
    account_id: int
    instruments: list[str] | str
    granularity: str = "M5"
    params: dict[str, Any] = Field(default_factory=dict)
    risk: dict[str, Any] = Field(default_factory=dict)
    ai_enabled: bool = False
    llm_profile_id: int | None = None
    notes: str = ""
    change_note: str = Field(default="", max_length=200)

    def to_record(self, status: StrategyStatus = StrategyStatus.ACTIVE) -> StrategyRecord:
        instruments = self.instruments.split(",") if isinstance(self.instruments, str) else self.instruments
        return StrategyRecord(
            name=self.name, kind=self.kind, account_id=self.account_id,
            instruments=[i for i in (x.strip() for x in instruments) if i], granularity=self.granularity,
            params=validate_params(self.kind, self.params), risk=RiskConfig.model_validate(self.risk),
            ai_enabled=self.ai_enabled, llm_profile_id=self.llm_profile_id, status=status, notes=self.notes)


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    status: StrategyStatus


class DuplicateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    new_name: str


class CommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["close_trade", "close_strategy", "check_account"]
    target: str = Field(min_length=1, max_length=60)


class DefaultIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int


class BadRequest(ValueError):
    pass


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def _closed(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in trades if t["status"] == "closed" and t["pnl"] is not None]


def _by_instrument(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[t["instrument"]].append(t)
    rows = []
    for inst, ts in groups.items():
        pnls = [t["pnl"] for t in ts]
        pips = [t["pips"] for t in ts if t["pips"] is not None]
        rows.append({"instrument": inst, "trades": len(ts), "net_pnl": round(sum(pnls), 2),
                     "pips": round(sum(pips), 1), "win_rate": round(sum(p > 0 for p in pnls) / len(pnls) * 100, 1)})
    return sorted(rows, key=lambda r: r["net_pnl"], reverse=True)


def _stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = _closed(trades)
    rows = [{"pnl": t["pnl"], "opened_at": t["opened_at"], "closed_at": t["closed_at"]} for t in closed]
    out = trade_stats(rows)
    pips = [t["pips"] for t in closed if t["pips"] is not None]
    out["net_pips"] = round(sum(pips), 1) if pips else None
    out["avg_pips"] = round(sum(pips) / len(pips), 1) if pips else None
    return out


def _curve(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total, out = 0.0, []
    for t in sorted(_closed(trades), key=lambda t: t["closed_at"]):
        total += t["pnl"]
        out.append({"t": t["closed_at"], "equity": round(total, 2)})
    return out


def _daily(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    days: dict[str, float] = defaultdict(float)
    for t in _closed(trades):
        days[t["closed_at"][:10]] += t["pnl"]
    return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(days.items())]


def _public_trade(t: dict[str, Any]) -> dict[str, Any]:
    return {k: t[k] for k in ("id", "strategy", "strategy_version", "account_id", "instrument", "direction", "units",
                              "status", "requested_price", "entry_price", "sl_price", "tp_price", "opened_at",
                              "exit_price", "closed_at", "pnl", "pips", "close_reason", "signal_reason", "ai_model",
                              "error", "created_at")}


class ConsoleService:
    def __init__(self, settings: FxSettings, store: FxStore, box: SecretBox | None) -> None:
        self.settings = settings
        self.store = store
        self.box = box   # None = master key missing: console is read-only for secrets

    # ------------------------------------------------------------------ reads
    def engine(self) -> dict[str, Any]:
        hb = self.store.kv_get("engine") or {}
        state = "never_started"
        if hb:
            age = (datetime.now(UTC) - datetime.fromisoformat(hb["ts"])).total_seconds()
            state = hb.get("state", "running") if age <= ENGINE_STALE_S else "not_reporting"
            hb["age_s"] = round(age)
        hb["status"] = state
        return hb

    def _account_labels(self) -> dict[int, dict[str, Any]]:
        return {a["id"]: a for a in self.store.list_accounts()}

    def _llm_labels(self) -> dict[int, dict[str, Any]]:
        return {p["id"]: p for p in self.store.list_llm_profiles()}

    def strategies(self) -> dict[str, Any]:
        engine = self.engine()
        accounts, llms = self._account_labels(), self._llm_labels()
        default_llm = next((p for p in llms.values() if p["is_default"]), None)
        now = datetime.now(UTC)
        day_start = fx_risk.trading_day_start(now).isoformat()
        month = _since(30)
        rows = []
        for rec in self.store.list_strategies():
            trades = self.store.trades(strategy=rec.name, limit=100_000)
            closed = _closed(trades)
            llm = llms.get(rec.llm_profile_id) if rec.llm_profile_id else default_llm
            live = engine.get("strategies", {}).get(rec.name, {})
            rows.append({
                **rec.model_dump(mode="json"),
                "account": accounts.get(rec.account_id, {}).get("label"),
                "currency": accounts.get(rec.account_id, {}).get("currency"),
                "ai_model": (llm or {}).get("model") if rec.ai_enabled else None,
                "ai_label": (llm or {}).get("label") if rec.ai_enabled else None,
                "stats": _stats(trades),
                "pnl_30d": round(sum(t["pnl"] for t in closed if t["closed_at"] >= month), 2),
                "pnl_today": round(sum(t["pnl"] for t in closed if t["closed_at"] >= day_start), 2),
                "open_trades": sum(t["status"] == "open" for t in trades),
                "last_bar": live.get("last_bar"),
                "halted": bool(live.get("halted")),
                "error": engine.get("invalid", {}).get(rec.name),
                "spark": [p["equity"] for p in _curve(trades)][-40:],
            })
        return {"strategies": rows}

    def overview(self) -> dict[str, Any]:
        engine = self.engine()
        strategies = self.strategies()["strategies"]
        accounts = self._account_labels()
        open_rows = self.store.open_trades()
        day_start = fx_risk.trading_day_start(datetime.now(UTC)).isoformat()
        today = [t for t in self.store.trades(status="closed", since=day_start, limit=10_000)]
        by_ccy: dict[str, dict[str, float]] = defaultdict(lambda: {"balance": 0.0, "nav": 0.0, "unrealized": 0.0,
                                                                     "today": 0.0})
        for acc_id, a in (engine.get("accounts") or {}).items():
            s = a.get("summary") or {}
            if not s:
                continue
            c = by_ccy[s.get("currency") or "?"]
            c["balance"] += s.get("balance", 0)
            c["nav"] += s.get("nav", 0)
            c["unrealized"] += s.get("unrealized_pl", 0)
        for t in today:
            ccy = accounts.get(t["account_id"], {}).get("currency") or "?"
            by_ccy[ccy]["today"] += t["pnl"] or 0
        return {
            "engine": {k: engine.get(k) for k in ("status", "ts", "age_s", "uptime_s", "session", "allow_live")},
            "session": fx_risk.fx_session(datetime.now(UTC)).value,
            "totals": {k: {kk: round(vv, 2) for kk, vv in v.items()} for k, v in by_ccy.items()},
            "counts": {"strategies": sum(s["status"] != "retired" for s in strategies),
                       "active": sum(s["status"] == "active" for s in strategies),
                       "open_trades": len(open_rows), "trades_today": len(today),
                       "accounts": len(accounts), "llms": len(self.store.list_llm_profiles())},
            "leaderboard": sorted((s for s in strategies if s["status"] != "retired"),
                                  key=lambda s: s["pnl_30d"], reverse=True),
            "open_trades": [_public_trade(t) | {"account": accounts.get(t["account_id"], {}).get("label")}
                            for t in open_rows],
            "recent": self.store.events(limit=15),
        }

    def strategy(self, name: str) -> dict[str, Any]:
        rec = self.store.get_strategy(name)
        if rec is None:
            raise BadRequest("unknown strategy")
        trades = self.store.trades(strategy=rec.name, limit=100_000)
        versions = self.store.strategy_versions(rec.name)
        per_version = []
        for v in versions:
            vt = [t for t in trades if t["strategy_version"] == v["version"]]
            per_version.append({**v, "stats": _stats(vt)})
        return {
            "strategy": next(s for s in self.strategies()["strategies"] if s["name"].lower() == rec.name.lower()),
            "versions": per_version,
            "by_instrument": _by_instrument(_closed(trades)),
            "curve": _curve(trades),
            "daily": _daily(trades)[-60:],
            "trades": [_public_trade(t) for t in trades[:300]],
            "events": self.store.events(strategy=rec.name, limit=150),
        }

    def trades(self, strategy: str | None, status: str | None, days: int) -> dict[str, Any]:
        accounts = self._account_labels()
        rows = self.store.trades(strategy=strategy or None, status=status or None, since=_since(days), limit=2000)
        return {"trades": [_public_trade(t) | {"account": accounts.get(t["account_id"], {}).get("label")}
                           for t in rows], "stats": _stats(rows)}

    def events(self, strategy: str | None, limit: int) -> dict[str, Any]:
        return {"events": self.store.events(strategy=strategy or None, limit=limit)}

    def accounts(self) -> dict[str, Any]:
        live = (self.engine().get("accounts") or {})
        rows = []
        for a in self.store.list_accounts():
            hb = live.get(str(a["id"]), {})
            rows.append(a | {"connected": hb.get("connected", False), "stream": hb.get("stream"),
                             "summary": hb.get("summary"), "engine_error": hb.get("error"),
                             "hedging": None if a["hedging"] is None else bool(a["hedging"])})
        return {"accounts": rows, "allow_live": self.settings.allow_live, "can_store_secrets": self.box is not None}

    def llms(self) -> dict[str, Any]:
        return {"profiles": self.store.list_llm_profiles(), "can_store_secrets": self.box is not None}

    def meta(self) -> dict[str, Any]:
        return {
            "catalog": catalog(),
            "risk_schema": RiskConfig.model_json_schema(),
            "risk_defaults": RiskConfig().model_dump(),
            "granularities": list(GRANULARITY_SECONDS),
            "hard_limits": {"max_risk_pct": fx_risk.HARD_MAX_RISK_PCT, "max_units": fx_risk.HARD_MAX_UNITS,
                            "max_open_trades": fx_risk.HARD_MAX_OPEN_TRADES,
                            "max_spread_pips": fx_risk.HARD_MAX_SPREAD_PIPS,
                            "max_slippage_pips": fx_risk.HARD_MAX_SLIPPAGE_PIPS},
            "allow_live": self.settings.allow_live,
        }

    def command(self, command_id: int) -> dict[str, Any]:
        cmd = self.store.get_command(command_id)
        if cmd is None:
            raise BadRequest("unknown command")
        return cmd

    # ------------------------------------------------------------------ writes
    def _need_box(self) -> SecretBox:
        if self.box is None:
            raise BadRequest("FX_MASTER_KEY is not set on the server, so keys cannot be stored")
        return self.box

    def add_account(self, body: dict[str, Any]) -> dict[str, Any]:
        a = AccountIn.model_validate(body)
        box = self._need_box()
        account_id = self.store.add_account(label=a.label, environment=a.environment,
                                            oanda_account_id=a.oanda_account_id, token_enc=box.seal(a.token),
                                            token_hint=hint(a.token))
        cmd = self.store.enqueue_command("check_account", str(account_id))
        return {"id": account_id, "command": cmd}

    def save_llm(self, body: dict[str, Any]) -> dict[str, Any]:
        p = LlmIn.model_validate(body)
        enc = self._need_box().seal(p.api_key) if p.api_key else None
        if p.id is None:
            pid = self.store.add_llm_profile(label=p.label, model=p.model, api_key_enc=enc,
                                             api_key_hint=hint(p.api_key) if p.api_key else None,
                                             api_base=p.api_base, is_default=p.is_default)
        else:
            pid = p.id
            self.store.update_llm_profile(pid, model=p.model, api_key_enc=enc,
                                          api_key_hint=hint(p.api_key) if p.api_key else None, api_base=p.api_base)
            if p.is_default:
                self.store.set_default_llm(pid)
        return {"id": pid}

    def default_llm(self, body: dict[str, Any]) -> dict[str, Any]:
        self.store.set_default_llm(DefaultIn.model_validate(body).id)
        return {"ok": True}

    def create_strategy(self, body: dict[str, Any]) -> dict[str, Any]:
        rec = StrategyIn.model_validate(body).to_record()
        return {"strategy": self.store.create_strategy(rec).model_dump(mode="json")}

    def update_strategy(self, body: dict[str, Any]) -> dict[str, Any]:
        data = StrategyIn.model_validate(body)
        current = self.store.get_strategy(data.name)
        if current is None:
            raise BadRequest("unknown strategy")
        rec = data.to_record(status=current.status)
        return {"strategy": self.store.update_strategy(rec, data.change_note).model_dump(mode="json")}

    def set_status(self, body: dict[str, Any]) -> dict[str, Any]:
        s = StatusIn.model_validate(body)
        self.store.set_strategy_status(s.name, s.status)
        return {"ok": True}

    def duplicate(self, body: dict[str, Any]) -> dict[str, Any]:
        d = DuplicateIn.model_validate(body)
        src = self.store.get_strategy(d.name)
        if src is None:
            raise BadRequest("unknown strategy")
        copy = src.model_copy(update={"name": validate_name(d.new_name), "status": StrategyStatus.PAUSED,
                                      "version": 1, "notes": f"copy of {src.name}"})
        rec = StrategyRecord.model_validate(copy.model_dump())
        return {"strategy": self.store.create_strategy(rec, note=f"duplicated from {src.name} v{src.version}")
                .model_dump(mode="json")}

    def command_post(self, body: dict[str, Any]) -> dict[str, Any]:
        c = CommandIn.model_validate(body)
        return {"command": self.store.enqueue_command(c.kind, c.target)}


def _int(query: dict[str, list[str]], name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(query.get(name, [default])[0])))
    except ValueError:
        return default


def _q(query: dict[str, list[str]], name: str) -> str:
    return query.get(name, [""])[0]


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for e in exc.errors()[:4]:
            where = ".".join(str(x) for x in e["loc"])
            msg = e["msg"].removeprefix("Value error, ")
            parts.append(f"{where}: {msg}" if where else msg)
        return "; ".join(parts)
    return str(exc)


def make_handler(service: ConsoleService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "fxconsole"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            log_event(log, logging.DEBUG, "http", line=fmt % args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, default=str).encode(), "application/json")

        def _run(self, fn: Callable[[], Any]) -> None:
            try:
                self._json(HTTPStatus.OK, fn())
            except (BadRequest, StoreError, ValidationError, ValueError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": _error_text(exc)})
            except Exception as exc:
                log_event(log, logging.ERROR, "fx.console.error", path=self.path, error=repr(exc))
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error (see server log)"})

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            q = parse_qs(url.query)
            path = url.path.rstrip("/") or "/"
            routes: dict[str, Callable[[], Any]] = {
                "/api/overview": service.overview,
                "/api/strategies": service.strategies,
                "/api/strategy": lambda: service.strategy(_q(q, "name")),
                "/api/trades": lambda: service.trades(_q(q, "strategy"), _q(q, "status"), _int(q, "days", 30, 1, 3650)),
                "/api/events": lambda: service.events(_q(q, "strategy"), _int(q, "limit", 200, 1, 1000)),
                "/api/accounts": service.accounts,
                "/api/llms": service.llms,
                "/api/meta": service.meta,
                "/api/command": lambda: service.command(_int(q, "id", 0, 0, 2**62)),
                "/healthz": lambda: {"ok": True},
            }
            if path in routes:
                self._run(routes[path])
                return
            name = "index.html" if path == "/" else path.removeprefix("/static/")
            if name in STATIC_FILES:
                body = (STATIC_DIR / name).read_bytes()
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                self._send(HTTPStatus.OK, body, f"{ctype}; charset=utf-8")
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _same_origin(self) -> bool:
            if self.headers.get("X-FX-Request") != "1":
                return False
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            if origin:
                host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
                return urlsplit(origin).netloc.lower() == host.split(",")[0].strip().lower()
            return True

        def do_POST(self) -> None:
            path = urlsplit(self.path).path.rstrip("/")
            routes: dict[str, Callable[[dict[str, Any]], Any]] = {
                "/api/accounts": service.add_account,
                "/api/llms": service.save_llm,
                "/api/llms/default": service.default_llm,
                "/api/strategies": service.create_strategy,
                "/api/strategies/update": service.update_strategy,
                "/api/strategies/status": service.set_status,
                "/api/strategies/duplicate": service.duplicate,
                "/api/commands": service.command_post,
            }
            if path not in routes:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY:
                self.close_connection = True
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body missing or too large"})
                return
            raw = self.rfile.read(length)  # always drain the body before answering
            if not self._same_origin():
                self._json(HTTPStatus.FORBIDDEN, {"error": "cross-site request refused"})
                return
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON body required"})
                return
            try:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("JSON object expected")
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON: {exc}"})
                return
            log_event(log, logging.INFO, "fx.console.write", path=path)  # never log the body: it may hold keys
            self._run(lambda: routes[path](body))

        def do_PUT(self) -> None:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "use POST"})

        do_DELETE = do_PATCH = do_PUT

    return Handler


def main() -> None:
    load_dotenv(encoding="utf-8-sig")
    settings = FxSettings()
    setup_logging(settings.log_level, settings.log_json)
    key = settings.master_key.get_secret_value()
    box = SecretBox(key) if key else None
    if box is None:
        log_event(log, logging.WARNING, "fx.console.no_master_key",
                  hint="accounts and AI keys cannot be added until FX_MASTER_KEY is set")
    service = ConsoleService(settings, FxStore(settings.db_path), box)
    server = ThreadingHTTPServer((settings.dashboard_host, settings.dashboard_port), make_handler(service))
    log_event(log, logging.INFO, "fx.console.start", host=settings.dashboard_host, port=settings.dashboard_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
