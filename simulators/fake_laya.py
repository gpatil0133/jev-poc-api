"""Stub laya-serve for running the gateway and simulators without a GPU.

Same routes, limits and result shape as laya 0.3.27's laya/serve.py, including one
forward pass at a time and the 64-state batch cap. The answers are a hash of the
input (app/backends/fake.py): they exercise the plumbing and mean nothing.

    python -m uvicorn simulators.fake_laya:app --port 8000

FAKE_LAYA_BASE_MS / FAKE_LAYA_ROW_MS set the simulated cost of a forward pass
(defaults 30 ms + 6 ms per state x question row, roughly the published T4 figures).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.backends.fake import fake_result

MAX_BATCH_STATES = 64
MAX_QUESTIONS = 64
BASE_MS = float(os.environ.get("FAKE_LAYA_BASE_MS", "30"))
ROW_MS = float(os.environ.get("FAKE_LAYA_ROW_MS", "6"))
API_KEY = os.environ.get("LAYA_API_KEY") or None

app = FastAPI(title="fake laya-serve")
_gate: Optional[asyncio.Lock] = None


def _check_auth(authorization: Optional[str]) -> None:
    if API_KEY and authorization != f"Bearer {API_KEY}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


async def _forward(rows: int) -> float:
    """Hold the single inference gate for the simulated duration of one pass."""
    global _gate
    if _gate is None:
        _gate = asyncio.Lock()
    infer_ms = BASE_MS + ROW_MS * rows
    async with _gate:
        await asyncio.sleep(infer_ms / 1000.0)
    return infer_ms


def _questions(body: Any) -> dict[str, dict]:
    if not isinstance(body, dict) or not isinstance(body.get("questions"), dict):
        raise HTTPException(status_code=400, detail="request body must be an object with a 'questions' field")
    if len(body["questions"]) > MAX_QUESTIONS:
        raise HTTPException(status_code=413, detail="too many questions")
    return body["questions"]


@app.get("/health")
async def health(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    if API_KEY and authorization != f"Bearer {API_KEY}":
        return {"status": "ok"}
    return {"status": "ok", "loaded": ["english"], "revisions": {}, "device": "fake",
            "device_is_preference": False, "checkpoint_devices": {"english": "fake"}, "cpu_fallbacks": {}}


@app.post("/v1/systemone")
async def systemone(request: Request, authorization: Optional[str] = Header(default=None)):
    _check_auth(authorization)
    body = await request.json()
    questions = _questions(body)
    if body.get("state") is None:
        raise HTTPException(status_code=400, detail="'state' is required")
    infer_ms = await _forward(len(questions))
    return JSONResponse(fake_result(body["state"], questions),
                        headers={"X-Inference-Time-Ms": f"{infer_ms:.2f}"})


@app.post("/v1/systemone/batch")
async def systemone_batch(request: Request, authorization: Optional[str] = Header(default=None)):
    _check_auth(authorization)
    body = await request.json()
    questions = _questions(body)
    states = body.get("states")
    if not isinstance(states, list) or not states:
        raise HTTPException(status_code=400, detail="'states' must be a non-empty list")
    if len(states) > MAX_BATCH_STATES:
        raise HTTPException(status_code=413,
                            detail="too many states in batch (%d > %d)" % (len(states), MAX_BATCH_STATES))
    infer_ms = await _forward(len(states) * len(questions))
    results = [fake_result(state, questions) for state in states]
    return JSONResponse(
        {"results": results,
         "total_usage": {"input_tokens": sum(r["usage"]["input_tokens"] for r in results), "output_tokens": 0}},
        headers={"X-Inference-Time-Ms": f"{infer_ms:.2f}"})
