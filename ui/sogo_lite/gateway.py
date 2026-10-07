"""The only code that talks to the classification gateway (plan section 7).

Contract:
  - never raises to the caller; every call returns a GatewayResult with a `fallback` flag
  - connection failure, timeout, non-2xx, unparseable body and a 200 whose own
    `fallback` is true are all fallbacks
  - one row in gateway_call_log per call, without the token
  - the fault switches (section 9.2) are applied here, before sending
"""
from __future__ import annotations

import copy
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

import httpx

from sogo_lite import db
from sogo_lite.config import get_settings, gateway_token

logger = logging.getLogger(__name__)

FAULTS: list[dict[str, str]] = [
    {"id": "down", "name": "Gateway down",
     "effect": "The client skips the call and returns a connection-failure fallback"},
    {"id": "slow", "name": "Slow gateway",
     "effect": "The client waits the set number of milliseconds before sending"},
    {"id": "lowconf", "name": "Forced low confidence",
     "effect": "The client rewrites every answer's band to none after the response arrives"},
    {"id": "badtoken", "name": "Bad token",
     "effect": "The client sends an invalid token"},
]
INVALID_TOKEN = "invalid.sogo-lite.token"
HEALTH_TTL_S = 10.0

# The scenario runner applies faults for one run without touching the saved switches.
_fault_override: ContextVar[Optional[dict[str, Any]]] = ContextVar("fault_override", default=None)

_http: Optional[httpx.Client] = None
_health_cache: dict[str, Any] = {"at": 0.0, "value": None}
# Last thing the gateway said about the token; the header shows it.
auth_state: dict[str, str] = {"state": "unknown"}


@dataclass
class GatewayResult:
    fallback: bool
    reason: Optional[str] = None
    status: Optional[int] = None
    data: dict[str, Any] = field(default_factory=dict)
    log_id: int = 0
    client_ms: float = 0.0
    faults: list[str] = field(default_factory=list)

    @property
    def answers(self) -> dict[str, dict[str, Any]]:
        return self.data.get("answers") or {}

    @property
    def detail(self) -> dict[str, Any]:
        """The gateway's structured error body, when it sent one."""
        detail = self.data.get("detail")
        return detail if isinstance(detail, dict) else {}

    @property
    def message(self) -> str:
        if not self.fallback:
            return ""
        return str(self.detail.get("message") or self.reason or "no answer")


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(base_url=get_settings().gateway_base_url)
    return _http


# ── fault switches ──────────────────────────────────────────────────────────

def fault_settings() -> dict[str, Any]:
    """Scope per switch: '' (off), 'all', or one module id."""
    forced = _fault_override.get()
    if forced is not None:
        return {"down": "", "slow": "", "lowconf": "", "badtoken": "", "slow_ms": 0, **forced}
    out: dict[str, Any] = {f["id"]: db.get_setting(f"fault.{f['id']}", "") for f in FAULTS}
    try:
        out["slow_ms"] = int(db.get_setting("fault.slow_ms", "1000") or 0)
    except ValueError:
        out["slow_ms"] = 0
    return out


@contextmanager
def override_faults(forced: dict[str, Any]) -> Iterator[None]:
    token = _fault_override.set(forced)
    try:
        yield
    finally:
        _fault_override.reset(token)


def _active(module: str) -> dict[str, Any]:
    settings = fault_settings()
    parts = set(module.split("+"))
    active = {name: settings[name] == "all" or settings[name] in parts
              for name in ("down", "slow", "lowconf", "badtoken")}
    active["slow_ms"] = settings["slow_ms"] if active["slow"] else 0
    return active


def _force_low_confidence(data: dict[str, Any]) -> None:
    """Rewrite bands in place. `_forced` marks the answer so a rewritten result is
    never mistaken for a real one, and so readers with their own minimum probability
    also treat it as unsure."""
    groups = [data.get("answers")] + [r.get("answers") for r in data.get("results") or []]
    for answers in groups:
        for answer in (answers or {}).values():
            answer["_original_band"] = answer.get("band")
            answer["band"] = "none"
            answer["_forced"] = True


# ── the call ────────────────────────────────────────────────────────────────

def call(method: str, path: str, body: Optional[dict[str, Any]] = None, *, module: str,
         trigger: str, kind: str = "author", survey_no: Optional[int] = None,
         response_id: Optional[int] = None) -> GatewayResult:
    settings = get_settings()
    timeout_ms = settings.timeouts_ms[kind]
    faults = _active(module)
    applied: list[str] = []
    send: Optional[dict[str, Any]] = copy.deepcopy(body) if body is not None else None
    is_classify = path == "/v1/classify"
    if is_classify and send is not None:
        send["timeout_ms"] = timeout_ms

    status: Optional[int] = None
    data: Any = None
    reason: Optional[str] = None
    token = gateway_token()
    t0 = time.perf_counter()

    if not token:
        reason = "no_token"
        auth_state["state"] = "no_token"
    elif faults["down"]:
        applied.append("gateway_down")
        reason = "connection_failed"
    else:
        if faults["slow_ms"]:
            applied.append(f"slow_gateway:{faults['slow_ms']}ms")
            time.sleep(min(faults["slow_ms"], timeout_ms) / 1000.0)
            if faults["slow_ms"] >= timeout_ms:
                reason = "timeout"
        if reason is None:
            remaining_ms = max(20.0, timeout_ms - (time.perf_counter() - t0) * 1000.0)
            if is_classify and send is not None:
                send["timeout_ms"] = int(remaining_ms)
            if faults["badtoken"]:
                applied.append("bad_token")
            headers = {"Authorization": f"Bearer {INVALID_TOKEN if faults['badtoken'] else token}"}
            try:
                # Slightly longer than the gateway's own limit, so a hung connection
                # cannot outlast it.
                resp = _client().request(method, path, json=send, headers=headers,
                                         timeout=(remaining_ms + settings.http_margin_ms) / 1000.0)
            except httpx.TimeoutException:
                reason = "timeout"
            except httpx.HTTPError as exc:
                reason = "connection_failed"
                logger.warning("Gateway %s %s failed: %s", method, path, type(exc).__name__)
            else:
                status = resp.status_code
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if status == 401:
                    reason = "token_rejected"
                    if not faults["badtoken"]:
                        auth_state["state"] = "rejected"
                elif not 200 <= status < 300:
                    reason = f"http_{status}"
                elif not isinstance(data, dict):
                    reason = "bad_body"
                elif data.get("fallback") is True:
                    reason = f"gateway_fallback:{data.get('fallback_reason') or 'unknown'}"
                if status != 401 and not faults["badtoken"]:
                    auth_state["state"] = "ok"

    if not isinstance(data, dict):
        data = {} if data is None else {"body": data}
    if reason is None and faults["lowconf"] and (data.get("answers") or data.get("results")):
        applied.append("forced_low_confidence")
        _force_low_confidence(data)

    client_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    answers = data.get("answers") or {}
    cached = any(a.get("cached") for a in answers.values()) or bool(data.get("cache_hits"))
    truncated = bool(data.get("truncated")) or any(r.get("truncated") for r in data.get("results") or [])
    log_id = db.run(
        "INSERT INTO gateway_call_log(ts, module, trigger, method, endpoint, kind, survey_no, response_id,"
        " request, response, http_status, client_ms, latency_ms, backend_ms, fallback, fallback_reason,"
        " fault, question_hash, cached, truncated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        db.now(), module, trigger, method, path, kind, survey_no, response_id,
        db.dumps(send) if send is not None else None, db.dumps(data), status, client_ms,
        data.get("latency_ms"), data.get("backend_ms"), 1 if reason else 0, reason,
        ", ".join(applied) or None, data.get("question_hash"), 1 if cached else 0, 1 if truncated else 0)
    return GatewayResult(fallback=reason is not None, reason=reason, status=status, data=data,
                         log_id=log_id, client_ms=client_ms, faults=applied)


def set_outcome(log_id: Optional[int], outcome: str) -> None:
    """What the module did with the result; several readers may add to one call."""
    if not log_id:
        return
    current = db.val("SELECT outcome FROM gateway_call_log WHERE id=?", log_id)
    if current and outcome in current.split("; "):
        return
    db.run("UPDATE gateway_call_log SET outcome=? WHERE id=?",
           f"{current}; {outcome}" if current else outcome, log_id)


# ── health (status indicator; a probe, so it is not written to the call log) ──

def health(force: bool = False) -> dict[str, Any]:
    if not force and time.time() - _health_cache["at"] < HEALTH_TTL_S and _health_cache["value"]:
        return _health_cache["value"]
    try:
        resp = _client().get("/health", timeout=2.0)
        body = resp.json() if resp.status_code == 200 else {}
        laya = body.get("laya") or {}
        value = {"reachable": resp.status_code == 200, "status": body.get("status", f"http_{resp.status_code}"),
                 "backend": body.get("backend") or "laya",
                 "loaded": laya.get("loaded") or [], "device": laya.get("device"),
                 "tasks": body.get("tasks"), "cache_entries": body.get("cache_entries")}
    except (httpx.HTTPError, ValueError) as exc:
        value = {"reachable": False, "status": "unreachable", "error": type(exc).__name__, "backend": None,
                 "loaded": [], "device": None, "tasks": None, "cache_entries": None}
    value["token"] = "missing" if not gateway_token() else auth_state["state"]
    value["base_url"] = get_settings().gateway_base_url
    value["base_url_source"] = get_settings().gateway_url_source
    _health_cache.update(at=time.time(), value=value)
    return value


# ── summaries for the Inspector ─────────────────────────────────────────────

def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, -(-len(ordered) * pct // 100) - 1))
    return round(ordered[int(index)], 1)


def summarise(calls: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(calls)
    sent = [c["client_ms"] for c in calls if c["client_ms"] is not None]
    return {
        "calls": n,
        "fallback_rate": round(100.0 * sum(1 for c in calls if c["fallback"]) / n, 1) if n else None,
        "cache_hit_rate": round(100.0 * sum(1 for c in calls if c["cached"]) / n, 1) if n else None,
        "p50_ms": percentile(sent, 50),
        "p95_ms": percentile(sent, 95),
    }
