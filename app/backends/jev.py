"""httpx client for Jev on the TypeSafe API.

Wire shape checked against typesafe-sdk 0.7.2 (generated from api.typesafe.ai/openapi.json):
  POST /v1/systemone   {state, questions, model}  -> {model, answers, usage}
  GET  /v1/models      -> {models: [{name, description, release_date}]}

Where Jev differs from laya-serve, this class absorbs the difference so the rest of
the gateway sees one result shape:
  - no batch route: a batch is single calls sent concurrently
  - no `option_order`: a rotation is sent as the same question with its criteria reordered
  - no `answer_confidence`: banding uses the probability of the reported answer
  - no `routing`: the model name comes from the top-level `model`
  - no /health: /v1/models stands in for it
  - no inference-time header: `backend_ms` stays empty
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from app.backends.base import BackendError

logger = logging.getLogger(__name__)

# Laya checkpoint names a template or caller may carry; Jev has its own model list.
LAYA_MODEL_NAMES = {"auto", "english", "multilingual"}
RETRY_STATUSES = {429, 503}
BUSY_RETRIES = 3
HEALTH_TTL_S = 30.0


class JevBackend:
    name = "jev"
    # One state per upstream call; callers use this to count backend calls.
    max_batch_states = 1

    def __init__(self, base_url: str, api_key: str, default_model: str = "jev-latest",
                 batch_timeout_s: float = 60.0, max_concurrent: int = 8,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._default_model = default_model
        self._max_concurrent = max(1, max_concurrent)
        self._health: tuple[float, dict[str, Any]] = (0.0, {})
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(batch_timeout_s, connect=5.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def predict(
        self, state: Any, questions: dict[str, dict], model: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        # No retry here: the live path has a hard budget and falls back instead.
        return await self._system_one(state, questions, model, timeout_s=timeout_s, retries=0)

    async def predict_batch(
        self, states: list[Any], questions: dict[str, dict], model: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        gate = asyncio.Semaphore(self._max_concurrent)

        async def _one(state: Any) -> dict[str, Any]:
            async with gate:
                return await self._system_one(state, questions, model, timeout_s=None, retries=BUSY_RETRIES)

        return list(await asyncio.gather(*(_one(state) for state in states)))

    async def health(self) -> dict[str, Any]:
        at, cached = self._health
        if cached and time.time() - at < HEALTH_TTL_S:
            return cached
        try:
            resp = await self._client.get("/v1/models", timeout=5.0)
            resp.raise_for_status()
            names = [m.get("name") for m in resp.json().get("models") or []]
            value: dict[str, Any] = {"status": "ok", "loaded": names, "device": "typesafe api",
                                     "default_model": self._default_model}
        except Exception as exc:
            logger.warning("Jev health check failed: %s", exc)
            value = {"status": "down", "error": f"{type(exc).__name__}: {exc}".rstrip(": ")}
        self._health = (time.time(), value)
        return value

    async def _system_one(self, state: Any, questions: dict[str, dict], model: Optional[str],
                          timeout_s: Optional[float], retries: int) -> dict[str, Any]:
        body = {"state": state, "questions": {qid: _wire_question(q) for qid, q in questions.items()},
                "model": self._default_model if not model or model in LAYA_MODEL_NAMES else model}
        kwargs: dict[str, Any] = {} if timeout_s is None else {"timeout": timeout_s}
        for attempt in range(retries + 1):
            try:
                resp = await self._client.post("/v1/systemone", json=body, **kwargs)
            except httpx.HTTPError as exc:
                raise BackendError(f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code in RETRY_STATUSES and attempt < retries:
                await asyncio.sleep(_retry_after(resp))
                continue
            if resp.status_code != 200:
                raise BackendError(_detail(resp), status=resp.status_code)
            return _normalise(resp.json())
        raise BackendError("server busy", status=503)


def _wire_question(question: dict) -> dict:
    """Jev rejects unknown fields. A rotation's `option_order` becomes the order of the
    criteria themselves; a score's order is its meaning, so there it is only dropped."""
    wire = {k: v for k, v in question.items() if k != "option_order"}
    order = question.get("option_order")
    criteria = question.get("criteria")
    if order and isinstance(criteria, dict):
        labels = list(criteria)
        wire["criteria"] = {labels[i]: criteria[labels[i]] for i in order}
    return wire


def _normalise(result: dict[str, Any]) -> dict[str, Any]:
    """Give a Jev result the fields the gateway reads from a Laya one."""
    for answer in (result.get("answers") or {}).values():
        probabilities = answer.get("probabilities") or {}
        if probabilities and "answer_confidence" not in answer:
            answer["answer_confidence"] = max(float(p) for p in probabilities.values())
    result.setdefault("routing", {"model": result.get("model"), "reason": "jev"})
    return result


def _retry_after(resp: httpx.Response) -> float:
    try:
        if resp.headers.get("retry-after-ms"):
            return min(5.0, max(0.1, float(resp.headers["retry-after-ms"]) / 1000.0))
        return min(5.0, max(0.1, float(resp.headers.get("retry-after", "1"))))
    except ValueError:
        return 1.0


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return f"{resp.status_code}: {body.get('detail') or body.get('error') or body}"[:300]
    except Exception:
        return f"{resp.status_code}: {resp.text[:200]}"
