"""A stand-in for Laya with the same result shape and no model behind it.

Answers are a deterministic hash of (state, question, option): stable across calls,
spread across the confidence range, and meaningless. Used by the tests and by
simulators/fake_laya.py to exercise the plumbing without a GPU. Never use its
labels for anything.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
from typing import Any, Optional

from app.backends.base import BackendError


def _unit(seed: str) -> float:
    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big") / 2 ** 64


def fake_result(state: Any, questions: dict[str, dict]) -> dict[str, Any]:
    """One /v1/systemone result in laya-serve's shape."""
    state_text = state if isinstance(state, str) else json.dumps(state, sort_keys=True, ensure_ascii=False)
    answers: dict[str, dict] = {}
    for qid, q in questions.items():
        criteria = q.get("criteria") or {}
        qtype = q.get("type", "choice")
        labels = [str(i) for i in range(len(criteria))] if qtype == "score" else list(criteria)
        # Keyed by option, not slot, so option_order rotations agree with each other.
        seed = state_text + "|" + str(q.get("instructions"))
        sharpness = 1.0 + 7.0 * _unit(seed + "|sharp")
        logits = [sharpness * _unit(seed + "|" + label) for label in labels]
        peak = max(logits)
        exp = [math.exp(z - peak) for z in logits]
        probs = [e / sum(exp) for e in exp]
        best = max(range(len(probs)), key=probs.__getitem__)
        entropy = -sum(p * math.log(p) for p in probs if p > 0)
        confidence = 1.0 - entropy / math.log(len(probs)) if len(probs) > 1 else 1.0
        answer: dict[str, Any] = {
            "type": qtype,
            "probabilities": {label: round(p, 4) for label, p in zip(labels, probs)},
            "confidence": round(confidence, 4),
            "answer_confidence": round(probs[best], 4),
            "action": {"act_probability": 1.0},
        }
        if qtype == "score":
            answer["score"] = round(sum(i * p for i, p in enumerate(probs)), 4)
            answer["legend"] = {str(i): str(c) for i, c in enumerate(criteria)}
        elif qtype == "noul":
            answer["noul"] = round(probs[-1], 4)
        else:
            answer["choice"] = labels[best]
        answers[qid] = answer
    tokens = len(state_text.split()) + 20 * len(questions)
    return {
        "model": "laya-rl-agent",
        "answers": answers,
        "usage": {"input_tokens": tokens, "output_tokens": 0, "truncated": False},
        "routing": {"model": "english", "reason": "fake backend"},
    }


class FakeBackend:
    """In-process DecisionBackend for tests: counts calls, can be slow or down."""

    max_batch_states = 64

    def __init__(self, delay_s: float = 0.0, down: bool = False):
        self.delay_s = delay_s
        self.down = down
        self.predict_calls = 0
        self.batch_calls = 0
        self.states_seen = 0
        self.last_questions: dict[str, dict] = {}

    async def predict(self, state: Any, questions: dict[str, dict], model: Optional[str] = None,
                      timeout_s: Optional[float] = None) -> dict[str, Any]:
        self.predict_calls += 1
        self.states_seen += 1
        self.last_questions = questions
        if self.down:
            raise BackendError("ConnectError: connection refused")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return fake_result(state, questions)

    async def predict_batch(self, states: list[Any], questions: dict[str, dict],
                            model: Optional[str] = None) -> list[dict[str, Any]]:
        self.batch_calls += 1
        self.states_seen += len(states)
        self.last_questions = questions
        if self.down:
            raise BackendError("ConnectError: connection refused")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return [fake_result(state, questions) for state in states]

    async def health(self) -> dict[str, Any]:
        if self.down:
            return {"status": "down", "error": "connection refused"}
        return {"status": "ok", "loaded": ["english"], "device": "fake", "device_is_preference": False}
