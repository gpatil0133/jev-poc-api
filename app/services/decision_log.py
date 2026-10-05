"""JSONL decision log: one line per classified text, under logs/decisions/.

This is the hook for the accuracy eval this cut skips: every decision is kept
with the hashes needed to join it to a labelled set later. The comment text is
only written when LOG_TEXT=true.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.models.schemas import Answer

logger = logging.getLogger(__name__)


class DecisionLog:
    def __init__(self, log_dir: str, log_text: bool = False):
        self._dir = Path(log_dir) / "decisions"
        self._log_text = log_text
        self._lock = threading.Lock()

    def record(self, endpoint: str, tenant_id: str, state_hash: str, question_hash: str,
               answers: dict[str, Answer], text: str = "", model: Optional[str] = None,
               latency_ms: Optional[float] = None, fallback: Optional[str] = None) -> dict:
        entry = {
            "ts": round(time.time(), 3),
            "endpoint": endpoint,
            "tenant_id": tenant_id,
            "state_hash": state_hash,
            "question_hash": question_hash,
            "model": model,
            "latency_ms": latency_ms,
            "fallback": fallback,
            "answers": {
                qid: {"label": a.label, "confidence": a.confidence, "value": a.value,
                      "band": a.band, "cached": a.cached}
                for qid, a in answers.items()
            },
        }
        if self._log_text:
            entry["text"] = text
        return entry

    def _append(self, records: list[dict]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"decisions_{datetime.now(timezone.utc):%Y%m%d}.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as fh:
            for entry in records:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def write(self, records: list[dict]) -> None:
        if not records:
            return
        try:
            await asyncio.to_thread(self._append, records)
        except Exception as exc:
            # Logging must never fail a respondent-facing call.
            logger.error("Decision log write failed: %s", exc)
