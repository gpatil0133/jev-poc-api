"""httpx client for stock laya-serve.

Wire shape checked against laya 0.3.27 (laya/serve.py):
  POST /v1/systemone        {state, questions, model?}   -> {model, answers, usage, routing}
  POST /v1/systemone/batch  {states, questions, model?}  -> {results: [...], total_usage}
  GET  /health              -> {status, loaded, device, ...} (details need the bearer)
Limits enforced upstream: 64 states per batch, 64 questions, 100 options per
choice, 16 requests in flight (503 + Retry-After past that).

A Jev backend is this class with another base URL and key.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from app.backends.base import BackendError

logger = logging.getLogger(__name__)

MAX_BATCH_STATES = 64
BUSY_RETRIES = 3


class LayaBackend:
    max_batch_states = MAX_BATCH_STATES

    def __init__(self, base_url: str, api_key: str = "", batch_timeout_s: float = 60.0):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(batch_timeout_s, connect=2.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def predict(
        self, state: Any, questions: dict[str, dict], model: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> dict[str, Any]:
        body = _body({"state": state, "questions": questions}, model)
        # No retry here: the live path has a hard budget and falls back instead.
        resp = await self._post("/v1/systemone", body, timeout_s=timeout_s, retries=0)
        return _with_infer_ms(resp)

    async def predict_batch(
        self, states: list[Any], questions: dict[str, dict], model: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for start in range(0, len(states), MAX_BATCH_STATES):
            chunk = states[start:start + MAX_BATCH_STATES]
            body = _body({"states": chunk, "questions": questions, "sort_by_length": True}, model)
            resp = await self._post("/v1/systemone/batch", body, timeout_s=None, retries=BUSY_RETRIES)
            chunk_results = resp.json().get("results") or []
            if len(chunk_results) != len(chunk):
                raise BackendError(
                    f"batch returned {len(chunk_results)} results for {len(chunk)} states")
            results.extend(chunk_results)
        return results

    async def health(self) -> dict[str, Any]:
        try:
            resp = await self._client.get("/health", timeout=2.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Laya health check failed: %s", exc)
            return {"status": "down", "error": f"{type(exc).__name__}: {exc}".rstrip(": ")}

    async def _post(self, path: str, body: dict, timeout_s: Optional[float], retries: int) -> httpx.Response:
        kwargs: dict[str, Any] = {} if timeout_s is None else {"timeout": timeout_s}
        for attempt in range(retries + 1):
            try:
                resp = await self._client.post(path, json=body, **kwargs)
            except httpx.HTTPError as exc:
                raise BackendError(f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code == 503 and attempt < retries:
                # laya-serve refuses past its admission bound rather than queueing.
                await asyncio.sleep(_retry_after(resp))
                continue
            if resp.status_code != 200:
                raise BackendError(_detail(resp), status=resp.status_code)
            return resp
        raise BackendError("server busy", status=503)


def _body(body: dict, model: Optional[str]) -> dict:
    # "auto"/None: omit the field so laya-serve routes by language.
    if model and model != "auto":
        body["model"] = model
    return body


def _with_infer_ms(resp: httpx.Response) -> dict[str, Any]:
    result = resp.json()
    try:
        result["_inference_ms"] = float(resp.headers.get("X-Inference-Time-Ms", ""))
    except ValueError:
        pass
    return result


def _retry_after(resp: httpx.Response) -> float:
    try:
        return min(5.0, max(0.1, float(resp.headers.get("Retry-After", "1"))))
    except ValueError:
        return 1.0


def _detail(resp: httpx.Response) -> str:
    try:
        return f"{resp.status_code}: {resp.json().get('detail')}"
    except Exception:
        return f"{resp.status_code}: {resp.text[:200]}"
