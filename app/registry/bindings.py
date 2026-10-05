"""Layer 2: per-question bindings, one JSON file each under
data/bindings/{tenant_id}/{survey_no}/{question_id}.json."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from app.models.schemas import Binding

logger = logging.getLogger(__name__)


class BindingStore:
    def __init__(self, data_dir: str):
        self._root = Path(data_dir) / "bindings"
        # Bindings are read on every live call; the files only change through put().
        self._cache: dict[tuple[str, int, str], Optional[Binding]] = {}

    def _path(self, tenant_id: str, survey_no: int, question_id: str) -> Path:
        return self._root / tenant_id / str(survey_no) / f"{question_id}.json"

    def get(self, tenant_id: str, survey_no: int, question_id: str) -> Optional[Binding]:
        key = (tenant_id, survey_no, question_id)
        if key in self._cache:
            return self._cache[key]
        path = self._path(tenant_id, survey_no, question_id)
        binding: Optional[Binding] = None
        if path.exists():
            try:
                binding = Binding.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Unreadable binding %s: %s", path, exc)
        self._cache[key] = binding
        return binding

    def put(self, tenant_id: str, binding: Binding) -> None:
        path = self._path(tenant_id, binding.survey_no, binding.question_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(binding.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
        self._cache[(tenant_id, binding.survey_no, binding.question_id)] = binding
        logger.info("Saved binding tenant=%s survey=%s question=%s",
                    tenant_id, binding.survey_no, binding.question_id)
