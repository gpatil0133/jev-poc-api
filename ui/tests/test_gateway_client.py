"""The gateway client contract (plan section 7) and the fault switches (9.2)."""
from tests.helpers import TOKEN, calls, fresh

import os

import httpx

from sogo_lite import db, gateway
from sogo_lite.config import TOKEN_ENV

QUESTION = {"id": "topic", "type": "choice", "classes": {"price": "cost", "staff": "people"}}
BODY = {"text": "too expensive", "questions": [QUESTION]}


def _classify(module: str = "settings", kind: str = "author") -> gateway.GatewayResult:
    return gateway.call("POST", "/v1/classify", BODY, module=module, trigger="test", kind=kind)


def test_answer_is_returned_and_logged_without_the_token():
    fake = fresh()
    fake.pick["topic"] = "price"
    result = _classify()
    assert not result.fallback, f"Expected an answer but got fallback {result.reason}"
    assert result.answers["topic"]["label"] == "price", f"Got {result.answers}"
    assert fake.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
    sent = db.loads(calls()[-1]["request"])
    assert 1450 <= sent["timeout_ms"] <= 1500, f"Expected the author timeout in the body but got {sent}"
    stored = " ".join(str(v) for row in db.rows("SELECT * FROM gateway_call_log") for v in row.values())
    stored += " ".join(str(r["value"]) for r in db.rows("SELECT value FROM setting"))
    assert TOKEN not in stored, "The token must never reach the database"


def test_missing_token_is_a_no_token_fallback():
    fake = fresh()
    os.environ[TOKEN_ENV] = ""
    try:
        result = _classify()
    finally:
        os.environ[TOKEN_ENV] = TOKEN
    assert result.fallback and result.reason == "no_token", f"Got {result.reason}"
    assert not fake.requests, "Nothing should be sent without a token"
    assert calls()[-1]["fallback_reason"] == "no_token"


def test_every_failure_is_a_fallback_and_never_raises():
    fake = fresh()
    fake.status = 500
    assert _classify().reason == "http_500"
    fake.status = 401
    assert _classify().reason == "token_rejected"
    assert gateway.auth_state["state"] == "rejected", f"Got {gateway.auth_state}"
    fake.status = 200
    fake.fallback = True
    assert _classify().reason == "gateway_fallback:timeout"
    fake.fallback = False
    fake.raise_error = httpx.ReadTimeout("slow")
    assert _classify().reason == "timeout"
    fake.raise_error = httpx.ConnectError("refused")
    assert _classify().reason == "connection_failed"
    assert len(calls(fallback=1)) == 5, f"Expected 5 logged fallbacks but got {len(calls(fallback=1))}"


def test_gateway_down_switch_skips_the_call():
    fake = fresh()
    with gateway.override_faults({"down": "all"}):
        result = _classify()
    assert result.fallback and result.reason == "connection_failed", f"Got {result.reason}"
    assert not fake.requests, "The client must skip the call when the switch is on"
    assert calls()[-1]["fault"] == "gateway_down"


def test_fault_scope_is_one_module_or_all():
    fake = fresh()
    with gateway.override_faults({"down": "logic_text"}):
        assert not _classify(module="settings").fallback, "A switch scoped to another module must not apply"
        assert _classify(module="logic_text").fallback
        assert _classify(module="sensitive_hint+logic_text").fallback, "A shared call takes either module's fault"
    assert len(fake.requests) == 1, f"Expected 1 request sent but got {len(fake.requests)}"


def test_slow_gateway_above_the_timeout_falls_back_inside_the_budget():
    fake = fresh()
    with gateway.override_faults({"slow": "all", "slow_ms": 5000}):
        result = _classify(kind="respondent")
    assert result.fallback and result.reason == "timeout", f"Got {result.reason}"
    assert not fake.requests, "A call that already ran out of time must not be sent"
    assert 250 <= result.client_ms < 700, f"Expected about the 300 ms respondent budget but took {result.client_ms}"


def test_forced_low_confidence_is_marked_as_rewritten():
    fake = fresh()
    fake.pick["topic"] = "price"
    with gateway.override_faults({"lowconf": "all"}):
        result = _classify()
    answer = result.answers["topic"]
    assert not result.fallback
    assert answer["band"] == "none" and answer["_forced"] and answer["_original_band"] == "act", f"Got {answer}"
    assert calls()[-1]["fault"] == "forced_low_confidence"


def test_bad_token_switch_sends_an_invalid_token():
    fake = fresh()
    with gateway.override_faults({"badtoken": "all"}):
        _classify()
    assert fake.requests[-1].headers["authorization"] == f"Bearer {gateway.INVALID_TOKEN}"
    assert calls()[-1]["fault"] == "bad_token"
