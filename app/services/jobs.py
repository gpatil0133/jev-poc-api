"""Async job runner for whole-survey backfills.

In-process asyncio task, no queue: job state lives in this worker's memory and is
lost on restart. Results are written as JSONL so a restart loses progress, not output.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.backends.base import BackendError
from app.models.schemas import JobStatus, Thresholds
from app.registry.compiler import CompiledSet
from app.services.classify import ClassifyService

logger = logging.getLogger(__name__)

# Stop a job after this many chunks in a row fail; the backend is down, not flaky.
MAX_CONSECUTIVE_FAILURES = 3

Entry = tuple[CompiledSet, dict[str, Any], str]


class JobRunner:
    def __init__(self, service: ClassifyService, data_dir: str, chunk_size: int):
        self._service = service
        self._root = Path(data_dir) / "jobs"
        self._chunk_size = chunk_size
        self._jobs: dict[str, JobStatus] = {}
        self._owners: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def get(self, job_id: str, tenant_id: str) -> Optional[JobStatus]:
        if self._owners.get(job_id) != tenant_id:
            return None
        return self._jobs.get(job_id)

    def start(self, tenant_id: str, entries: list[Entry], ids: list[Optional[str]],
              consumer: Optional[Thresholds]) -> JobStatus:
        job_id = uuid.uuid4().hex[:16]
        path = self._root / tenant_id / f"job_{job_id}.jsonl"
        status = JobStatus(job_id=job_id, status="queued", total=len(entries), processed=0,
                           output_path=str(path))
        self._jobs[job_id] = status
        self._owners[job_id] = tenant_id
        self._tasks[job_id] = asyncio.create_task(self._run(status, path, tenant_id, entries, ids, consumer))
        logger.info("Job %s queued: tenant=%s items=%s", job_id, tenant_id, len(entries))
        return status

    async def _run(self, status: JobStatus, path: Path, tenant_id: str, entries: list[Entry],
                   ids: list[Optional[str]], consumer: Optional[Thresholds]) -> None:
        status.status = "running"
        status.started_at = time.time()
        failures = 0
        try:
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            for start in range(0, len(entries), self._chunk_size):
                chunk = entries[start:start + self._chunk_size]
                chunk_ids = ids[start:start + self._chunk_size]
                try:
                    results, stats = await self._service.classify_batch(
                        chunk, tenant_id, consumer=consumer, ids=chunk_ids, endpoint="job")
                    lines = [r.model_dump() for r in results]
                    status.cache_hits += stats.cache_hits
                    failures = 0
                except BackendError as exc:
                    failures += 1
                    logger.warning("Job %s chunk at %s failed: %s", status.job_id, start, exc)
                    lines = [{"id": item_id, "error": str(exc)} for item_id in chunk_ids]
                    status.failed_items += len(chunk)
                    if failures >= MAX_CONSECUTIVE_FAILURES:
                        raise
                await asyncio.to_thread(_append_lines, path, lines)
                status.processed += len(chunk)
                elapsed = time.time() - status.started_at
                status.items_per_s = round(status.processed / elapsed, 1) if elapsed > 0 else None
            status.status = "done"
        except Exception as exc:
            status.status = "failed"
            status.error = str(exc)
            logger.error("Job %s failed: %s", status.job_id, exc)
        finally:
            status.finished_at = time.time()
            self._tasks.pop(status.job_id, None)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()


def _append_lines(path: Path, lines: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
