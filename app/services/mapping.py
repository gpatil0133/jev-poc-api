"""Column mapping for imports.

field_type      fixed list, one straight `choice` (template column.field_type)
survey_question which survey question a column answers. A survey can have far more
                questions than fit the option budget, so an embedding shortlist cuts
                them to the top k plus "none" before Laya picks.

The shortlist uses the same model and scoring as tagging-system's EmbeddingMatcher
(all-MiniLM-L6-v2, cosine), with the same lightweight fallback when
sentence-transformers is not installed.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from typing import Optional

from app.models.schemas import (
    Column, ColumnMapping, MapColumnsRequest, MapColumnsResponse, QuestionSelector,
    QuestionSpec, SurveyQuestionRef,
)
from app.registry.compiler import SHORTLIST_ADVICE_OPTIONS
from app.services.classify import ClassifyService

logger = logging.getLogger(__name__)

FIELD_TYPE_TASK = "column.field_type"
MATCH_QUESTION_ID = "match"
NONE_KEY = "none"
MAX_SAMPLES_IN_STATE = 8
# Keeps nine options inside the 192-token option budget.
MAX_OPTION_CHARS = 90
LIGHTWEIGHT_DIMS = 256


class Embedder:
    """sentence-transformers when available; otherwise a hashed token embedding."""

    def __init__(self) -> None:
        self._model = None
        self.name = "lightweight-hash"
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            self.name = "all-MiniLM-L6-v2"
            logger.info("Embedder using sentence-transformers model")
        except Exception as exc:
            logger.warning("Falling back to lightweight embedding shortlist: %s", exc)

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self._model is not None:
            vectors = self._model.encode(texts, normalize_embeddings=True)
            return [list(map(float, row)) for row in vectors]
        return [_lightweight_embed(text) for text in texts]


def _lightweight_embed(text: str, dims: int = LIGHTWEIGHT_DIMS) -> list[float]:
    vec = [0.0] * dims
    for token in re.findall(r"[a-zA-Z0-9']+", text.lower()):
        # hashlib, not hash(): stable across processes.
        index = int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:4], "big") % dims
        vec[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vec))
    return [value / norm for value in vec] if norm else vec


def _cosine(vec_a: list[float], vec_b: list[float]) -> float:
    return sum(a * b for a, b in zip(vec_a, vec_b))


def _column_text(column: Column) -> str:
    return f"{column.name}: {', '.join(column.samples[:MAX_SAMPLES_IN_STATE])}"


def _clip(text: str, limit: int = MAX_OPTION_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class MappingService:
    def __init__(self, service: ClassifyService):
        self._service = service
        self._embedder: Optional[Embedder] = None
        self._embedder_lock = asyncio.Lock()

    async def _get_embedder(self) -> Embedder:
        async with self._embedder_lock:
            if self._embedder is None:
                # Model load is blocking I/O.
                self._embedder = await asyncio.to_thread(Embedder)
        return self._embedder

    async def shortlist(self, columns: list[Column], questions: list[SurveyQuestionRef],
                        top_k: int) -> tuple[list[list[SurveyQuestionRef]], str]:
        """Top-k candidate questions per column, best first."""
        embedder = await self._get_embedder()
        vectors = await asyncio.to_thread(
            embedder.encode, [q.text for q in questions] + [_column_text(c) for c in columns])
        q_vecs, c_vecs = vectors[:len(questions)], vectors[len(questions):]
        out: list[list[SurveyQuestionRef]] = []
        for c_vec in c_vecs:
            ranked = sorted(range(len(questions)), key=lambda j: _cosine(c_vec, q_vecs[j]), reverse=True)
            out.append([questions[j] for j in ranked[:top_k]])
        return out, embedder.name

    async def map_columns(self, req: MapColumnsRequest, tenant_id: str) -> MapColumnsResponse:
        t0 = time.perf_counter()
        embedding: Optional[str] = None
        shortlists: list[list[SurveyQuestionRef]] = [[] for _ in req.columns]
        entries = []

        if req.mode == "field_type":
            selector = QuestionSelector(tasks=[FIELD_TYPE_TASK], model=req.model)
            cset, _ = self._service.compile_for(selector, tenant_id)
            answer_id = FIELD_TYPE_TASK
            for column in req.columns:
                state = {"column_name": column.name,
                         "sample_values": ", ".join(column.samples[:MAX_SAMPLES_IN_STATE])}
                entries.append((cset, state, column.name))
        else:
            answer_id = MATCH_QUESTION_ID
            if len(req.survey_questions) > SHORTLIST_ADVICE_OPTIONS:
                shortlists, embedding = await self.shortlist(req.columns, req.survey_questions, req.top_k)
            else:
                shortlists = [list(req.survey_questions) for _ in req.columns]
            for column, candidates in zip(req.columns, shortlists):
                classes = {q.id: _clip(q.text) for q in candidates}
                classes[NONE_KEY] = "none of these questions"
                spec = QuestionSpec(
                    id=MATCH_QUESTION_ID, type="choice",
                    instructions="Which survey question does this spreadsheet column hold the answers to?",
                    classes=classes,
                )
                cset, _ = self._service.compile_for(
                    QuestionSelector(questions=[spec], model=req.model), tenant_id)
                state = {"column_name": column.name,
                         "sample_values": ", ".join(column.samples[:MAX_SAMPLES_IN_STATE])}
                entries.append((cset, state, column.name))

        results, _ = await self._service.classify_batch(
            entries, tenant_id, consumer=req.thresholds, endpoint="map_columns")
        mappings: list[ColumnMapping] = []
        for column, result, candidates in zip(req.columns, results, shortlists):
            answer = result.answers[answer_id]
            is_none = answer.label in (None, NONE_KEY, "other")
            mappings.append(ColumnMapping(
                column=column.name,
                target=None if is_none else answer.label,
                confidence=answer.confidence,
                band=answer.band,
                probabilities=answer.probabilities,
                shortlist=[q.id for q in candidates],
            ))
        return MapColumnsResponse(mode=req.mode, mappings=mappings, embedding=embedding,
                                  latency_ms=round((time.perf_counter() - t0) * 1000.0, 2))
