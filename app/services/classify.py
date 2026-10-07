"""Cache lookup, backend call, banding and the timeout fallback.

One raw result is stored per (state hash, model, compiled-question hash). Bands are
worked out when the result is read, so branching can read the same record at 0.80
and an alert at 0.90.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from app.backends.base import BackendError, DecisionBackend
from app.core.config import Settings
from app.models.schemas import (
    Answer, BatchResult, ClassifyResponse, QuestionSelector, Thresholds,
)
from app.registry.bindings import BindingStore
from app.registry.compiler import (
    NO_KEY, ROTATION_SEP, YES_KEY, CompiledQuestion, CompiledSet, build_state,
    compile_set, state_hash,
)
from app.registry.loader import TaskRegistry
from app.services.decision_log import DecisionLog

logger = logging.getLogger(__name__)

# Concurrent backend calls for one batch request (distinct question sets).
GROUP_CONCURRENCY = 4
# Held back from the live budget for the gateway's own work, so the caller sees
# an answer or a fallback inside timeout_ms.
LIVE_MARGIN_MS = 20


class ResultCache:
    """In-process LRU. Local to the PoC; the platform would persist labels instead."""

    def __init__(self, max_entries: int):
        self._max = max_entries
        self._data: OrderedDict[tuple[str, str, str], dict] = OrderedDict()

    def get(self, key: tuple[str, str, str]) -> Optional[dict]:
        raw = self._data.get(key)
        if raw is not None:
            self._data.move_to_end(key)
        return raw

    def put(self, key: tuple[str, str, str], raw: dict) -> None:
        self._data[key] = raw
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


@dataclass
class BatchStats:
    groups: int = 0
    backend_calls: int = 0
    cache_hits: int = 0


# ── decoding and banding ────────────────────────────────────────────────────

def decode(q: CompiledQuestion, wire_answers: dict[str, dict]) -> Optional[dict]:
    """Turn Laya's answer(s) for one question into the stored raw result. When the
    question was sent under several option orders the probabilities are averaged."""
    variants = [a for key, a in wire_answers.items()
                if key == q.qid or key.startswith(q.qid + ROTATION_SEP)]
    if not variants:
        return None
    probs: dict[str, float] = {}
    for answer in variants:
        for label, p in (answer.get("probabilities") or {}).items():
            probs[label] = probs.get(label, 0.0) + float(p) / len(variants)
    if not probs:
        return None
    top = max(probs, key=probs.get)
    if len(variants) == 1:
        # `answer_confidence` is Laya's calibrated P(reported answer); `confidence` on a
        # choice is 1 - normalised entropy and moves with the option count.
        confidence = float(variants[0].get("answer_confidence", variants[0].get("confidence", probs[top])))
    else:
        confidence = probs[top]

    if q.kind == "yesno":
        p_yes = probs.get(YES_KEY, 0.0)
        return {"label": "yes" if p_yes >= 0.5 else "no", "confidence": round(max(p_yes, 1.0 - p_yes), 4),
                "probabilities": {"yes": round(p_yes, 4), "no": round(probs.get(NO_KEY, 1.0 - p_yes), 4)},
                "value": round(p_yes, 4)}
    if q.kind == "score":
        expected = sum(int(level) * p for level, p in probs.items())
        return {"label": str(int(round(expected))), "confidence": round(confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in probs.items()}, "value": round(expected, 4)}
    return {"label": top, "confidence": round(confidence, 4),
            "probabilities": {k: round(v, 4) for k, v in probs.items()}, "value": None}


def band_for(q: CompiledQuestion, raw: dict, defaults: dict[str, float],
             consumer: Optional[Thresholds] = None) -> str:
    """act / suggest / none. `none` means keep today's behaviour."""
    thresholds = {**defaults, **q.thresholds}
    if consumer:
        thresholds.update(consumer.model_dump(exclude_none=True))
    if q.kind == "yesno":
        # Banded on P(yes): a confident "no" is not something to act on.
        metric = raw["value"]
    elif q.kind == "choice" and raw["label"] == q.escape:
        return "none"
    else:
        metric = raw["confidence"]
    if metric >= thresholds["act"]:
        return "act"
    if metric >= thresholds["suggest"]:
        return "suggest"
    return "none"


def _answer(q: CompiledQuestion, raw: Optional[dict], cached: bool, defaults: dict[str, float],
            consumer: Optional[Thresholds]) -> Answer:
    if raw is None:
        return Answer()
    return Answer(label=raw["label"], confidence=raw["confidence"], probabilities=raw["probabilities"],
                  value=raw["value"], band=band_for(q, raw, defaults, consumer), cached=cached)


def _routed_model(result: dict) -> Optional[str]:
    return (result.get("routing") or {}).get("model")


def _truncated(result: dict) -> bool:
    return bool((result.get("usage") or {}).get("truncated"))


def _token_usage(result: dict) -> dict[str, int]:
    return {k: v for k, v in (result.get("usage") or {}).items()
            if k.endswith("_tokens") and isinstance(v, int)}


class ClassifyService:
    def __init__(self, backend: DecisionBackend, registry: TaskRegistry, bindings: BindingStore,
                 settings: Settings, decision_log: DecisionLog):
        self.backend = backend
        self.registry = registry
        self.bindings = bindings
        self.settings = settings
        self.decision_log = decision_log
        self.cache = ResultCache(settings.cache_max_entries)
        self._live_inflight = 0
        self._log_tasks: set[asyncio.Task] = set()

    def _model_key(self, cset: CompiledSet) -> str:
        """Cache namespace: an answer from one backend is never served as another's."""
        return f"{getattr(self.backend, 'name', 'laya')}:{cset.model}"

    def _log_later(self, records: list[dict]) -> None:
        """Write the decision log off the response path."""
        task = asyncio.create_task(self.decision_log.write(records))
        self._log_tasks.add(task)
        task.add_done_callback(self._log_tasks.discard)

    # ── question resolution ────────────────────────────────────────────────

    def compile_for(self, selector: QuestionSelector, tenant_id: str, question_id: Optional[str] = None,
                    rotations: Optional[int] = None) -> tuple[CompiledSet, Optional[str]]:
        """Compile the three layers for one call. Returns the set and the bound question text."""
        question_id = question_id or selector.question_id
        binding = None
        if selector.survey_no is not None and question_id:
            binding = self.bindings.get(tenant_id, selector.survey_no, question_id)
        cset = compile_set(
            self.registry, tasks=selector.tasks, binding=binding, inline=selector.questions,
            model=selector.model, default_model=self.settings.laya_default_model, rotations=rotations,
        )
        return cset, (binding.survey_question if binding else None)

    @staticmethod
    def state_for(cset: CompiledSet, text: str, survey_question: Optional[str],
                  metric_score: Optional[float], context: dict[str, Any]) -> dict[str, Any]:
        values = {**context, "answer": text.strip(), "survey_question": survey_question,
                  "metric_score": metric_score}
        return build_state(cset.state_fields, values)

    # ── live / per-response ────────────────────────────────────────────────

    async def classify_one(self, cset: CompiledSet, state: dict[str, Any], tenant_id: str,
                           timeout_ms: Optional[int] = None,
                           consumer: Optional[Thresholds] = None,
                           text: str = "") -> ClassifyResponse:
        """Never raises for a backend problem: on timeout or Laya down every missing
        answer comes back as band `none` with fallback=true."""
        t0 = time.perf_counter()
        defaults = self.settings.default_thresholds
        budget_ms = timeout_ms or self.settings.live_timeout_ms
        budget_s = (budget_ms - min(LIVE_MARGIN_MS, budget_ms / 2)) / 1000.0
        max_inflight = self.settings.live_max_inflight
        shash = state_hash(state)
        raws: dict[str, Optional[dict]] = {}
        cached: set[str] = set()
        fallback_reason: Optional[str] = None
        model: Optional[str] = None
        truncated = False
        backend_ms: Optional[float] = None
        usage: dict[str, int] = {}

        if not text.strip():
            fallback_reason = "empty_text"
        else:
            for q in cset.questions:
                hit = self.cache.get((shash, self._model_key(cset), q.qhash))
                if hit is not None:
                    raws[q.qid] = hit
                    cached.add(q.qid)
                    model = model or hit.get("model")
            missing = {q.qid for q in cset.questions if q.qid not in raws}
            if missing and max_inflight and self._live_inflight >= max_inflight:
                # Shed instead of queueing: a call Laya cannot start inside the budget
                # would time out anyway and still cost a forward pass.
                fallback_reason = "busy"
            elif missing:
                remaining = budget_s - (time.perf_counter() - t0)
                self._live_inflight += 1
                try:
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    # Live calls use one pass: rotations cost latency.
                    result = await asyncio.wait_for(
                        self.backend.predict(state, cset.wire_questions(missing), cset.model,
                                             timeout_s=remaining),
                        timeout=remaining,
                    )
                    model = _routed_model(result) or model
                    truncated = _truncated(result)
                    backend_ms = result.get("_inference_ms")
                    usage = _token_usage(result)
                    for q in cset.questions:
                        if q.qid in missing:
                            raw = decode(q, result.get("answers") or {})
                            if raw is not None:
                                raw["model"] = model
                                self.cache.put((shash, self._model_key(cset), q.qhash), raw)
                            raws[q.qid] = raw
                except asyncio.TimeoutError:
                    fallback_reason = "timeout"
                except BackendError as exc:
                    fallback_reason = "timeout" if "Timeout" in str(exc) else "backend_error"
                    logger.warning("Laya call failed, falling back: %s", exc)
                except Exception as exc:
                    fallback_reason = "backend_error"
                    logger.error("Unexpected classify failure, falling back: %s", exc)
                finally:
                    self._live_inflight -= 1

        answers = {q.qid: _answer(q, raws.get(q.qid), q.qid in cached, defaults, consumer)
                   for q in cset.questions}
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        response = ClassifyResponse(
            answers=answers, fallback=fallback_reason is not None, fallback_reason=fallback_reason,
            latency_ms=latency_ms, backend_ms=backend_ms, model=model, truncated=truncated,
            question_hash=cset.set_hash, usage=usage,
        )
        self._log_later([self.decision_log.record(
            "classify", tenant_id, shash, cset.set_hash, answers, text=text, model=model,
            latency_ms=latency_ms, fallback=fallback_reason,
        )])
        return response

    # ── batch ──────────────────────────────────────────────────────────────

    async def classify_batch(
        self, entries: list[tuple[CompiledSet, dict[str, Any], str]], tenant_id: str,
        consumer: Optional[Thresholds] = None, ids: Optional[list[Optional[str]]] = None,
        endpoint: str = "batch",
    ) -> tuple[list[BatchResult], BatchStats]:
        """entries: (compiled set, state, text) per item. Items are grouped by compiled hash
        so each distinct question set is one backend batch. Raises BackendError."""
        t0 = time.perf_counter()
        defaults = self.settings.default_thresholds
        stats = BatchStats()
        results: list[Optional[BatchResult]] = [None] * len(entries)
        hashes = [state_hash(state) for _, state, _ in entries]

        groups: dict[str, list[int]] = {}
        for i, (cset, _, _) in enumerate(entries):
            groups.setdefault(cset.set_hash, []).append(i)
        stats.groups = len(groups)
        gate = asyncio.Semaphore(GROUP_CONCURRENCY)

        async def _run_group(indices: list[int]) -> None:
            cset = entries[indices[0]][0]
            raws: dict[int, dict[str, Optional[dict]]] = {i: {} for i in indices}
            cached: dict[int, set[str]] = {i: set() for i in indices}
            # Distinct uncached states only: repeated answers ("good", "n/a") are asked once.
            pending: dict[str, list[int]] = {}
            missing: set[str] = set()
            for i in indices:
                if not entries[i][2].strip():
                    continue
                for q in cset.questions:
                    hit = self.cache.get((hashes[i], self._model_key(cset), q.qhash))
                    if hit is not None:
                        raws[i][q.qid] = hit
                        cached[i].add(q.qid)
                    else:
                        missing.add(q.qid)
                if len(cached[i]) == len(cset.questions):
                    stats.cache_hits += 1
                else:
                    pending.setdefault(hashes[i], []).append(i)

            models: dict[int, Optional[str]] = {}
            truncated: dict[int, bool] = {}
            if pending:
                states = [entries[owners[0]][1] for owners in pending.values()]
                async with gate:
                    backend_results = await self.backend.predict_batch(
                        states, cset.wire_questions(missing, rotate=True), cset.model)
                stats.backend_calls += -(-len(states) // self.backend.max_batch_states)
                for (shash, owners), result in zip(pending.items(), backend_results):
                    model = _routed_model(result)
                    decoded: dict[str, Optional[dict]] = {}
                    for q in cset.questions:
                        if q.qid in missing:
                            raw = decode(q, result.get("answers") or {})
                            if raw is not None:
                                raw["model"] = model
                                self.cache.put((shash, self._model_key(cset), q.qhash), raw)
                            decoded[q.qid] = raw
                    for i in owners:
                        # A partly cached item keeps its cached answers.
                        raws[i] = {**decoded, **{k: v for k, v in raws[i].items() if k in cached[i]}}
                        models[i] = model
                        truncated[i] = _truncated(result)

            for i in indices:
                answers = {q.qid: _answer(q, raws[i].get(q.qid), q.qid in cached[i], defaults, consumer)
                           for q in cset.questions}
                model = models.get(i) or next(
                    (r.get("model") for r in raws[i].values() if r and r.get("model")), None)
                results[i] = BatchResult(id=ids[i] if ids else None, answers=answers, model=model,
                                         truncated=truncated.get(i, False))

        await asyncio.gather(*(_run_group(indices) for indices in groups.values()))

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        done = [r for r in results if r is not None]
        await self.decision_log.write([
            self.decision_log.record(endpoint, tenant_id, hashes[i], entries[i][0].set_hash, r.answers,
                                     text=entries[i][2], model=r.model,
                                     latency_ms=round(latency_ms / len(entries), 2), fallback=None)
            for i, r in enumerate(done)
        ])
        return done, stats
