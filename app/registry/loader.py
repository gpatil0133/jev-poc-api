"""Layer 1: platform task templates, loaded from registry/tasks/*.yaml."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

TASKS_DIR = Path(__file__).parent / "tasks"
TEMPLATE_TYPES = ("choice", "yesno", "score")
DEFAULT_STATE_FIELDS = ["survey_question", "answer", "metric_score"]


@dataclass(frozen=True)
class TaskTemplate:
    id: str
    type: str
    instructions: str
    criteria: Any                      # choice/yesno: {label: description}; score: [level, ...]
    state: list[str] = field(default_factory=lambda: list(DEFAULT_STATE_FIELDS))
    model: str = "auto"
    thresholds: dict[str, float] = field(default_factory=dict)
    rotations: int = 1
    description: str = ""


class TaskRegistry:
    def __init__(self, templates: dict[str, TaskTemplate]):
        self._templates = templates

    def get(self, task_id: str) -> Optional[TaskTemplate]:
        return self._templates.get(task_id)

    def ids(self) -> list[str]:
        return sorted(self._templates)

    def all(self) -> list[TaskTemplate]:
        return [self._templates[i] for i in self.ids()]


def _parse_template(raw: dict, source: str) -> TaskTemplate:
    for key in ("id", "type", "instructions", "criteria"):
        if key not in raw:
            raise ValueError(f"{source}: template is missing '{key}'")
    ttype = raw["type"]
    if ttype not in TEMPLATE_TYPES:
        raise ValueError(f"{source}: type must be one of {TEMPLATE_TYPES}, got {ttype!r}")
    criteria = raw["criteria"]
    if ttype == "score" and not isinstance(criteria, list):
        raise ValueError(f"{source}: score criteria must be an ordered list")
    if ttype == "choice" and not isinstance(criteria, dict):
        raise ValueError(f"{source}: choice criteria must be a mapping")
    if ttype == "yesno" and (not isinstance(criteria, dict) or set(map(str, criteria)) != {"true", "false"}):
        raise ValueError(f"{source}: yesno criteria must have exactly the keys 'true' and 'false'")
    if isinstance(criteria, dict):
        criteria = {str(k): str(v) for k, v in criteria.items()}
    else:
        criteria = [str(c) for c in criteria]
    return TaskTemplate(
        id=str(raw["id"]),
        type=ttype,
        instructions=str(raw["instructions"]),
        criteria=criteria,
        state=[str(f) for f in raw.get("state") or DEFAULT_STATE_FIELDS],
        model=str(raw.get("model", "auto")),
        thresholds={k: float(v) for k, v in (raw.get("thresholds") or {}).items()},
        rotations=int(raw.get("rotations", 1)),
        description=str(raw.get("description", "")),
    )


def load_registry(tasks_dir: Path = TASKS_DIR) -> TaskRegistry:
    """Load every *.yaml under tasks_dir. A file holds one template or a list of them."""
    templates: dict[str, TaskTemplate] = {}
    for path in sorted(tasks_dir.glob("*.yaml")):
        docs = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if isinstance(docs, dict):
            docs = [docs]
        for raw in docs:
            template = _parse_template(raw, path.name)
            if template.id in templates:
                raise ValueError(f"{path.name}: duplicate template id {template.id!r}")
            templates[template.id] = template
    logger.info("Loaded %s task templates from %s", len(templates), tasks_dir)
    return TaskRegistry(templates)
