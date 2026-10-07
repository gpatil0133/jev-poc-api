"""The eight Design scopes as switchable modules (plan section 4.1).

A module registers event handlers and extension points when it is imported. Turning
it off stops its handlers and hides its panels; its stored data stays.
"""
from __future__ import annotations

import importlib
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from sogo_lite import db

MODULES: list[dict[str, str]] = [
    {"id": "shared_classes", "n": "1", "name": "Shared classes for branching and alerts",
     "where": "Design → question editor → Classes"},
    {"id": "alert_meaning", "n": "2", "name": "Rules & Alerts on comment meaning",
     "where": "Design → Rules & Alerts"},
    {"id": "logic_text", "n": "3", "name": "Logic on text answers",
     "where": "Design → Logic, live survey page"},
    {"id": "sensitive_hint", "n": "4", "name": "Builder hints for sensitive questions",
     "where": "Design → question editor"},
    {"id": "followup_flag", "n": "5", "name": "Follow-up flag on Text Box questions",
     "where": "Design → Text Box editor"},
    {"id": "tag_suggest", "n": "6", "name": "Tag suggestions",
     "where": "Design → Tags"},
    {"id": "template_pick", "n": "7", "name": "Create with AI: prompt template pick",
     "where": "Create with AI"},
    {"id": "quiz_scoring", "n": "8", "name": "Open-ended quiz scoring",
     "where": "Assign Scores, grading list"},
]
MODULE_IDS = [m["id"] for m in MODULES]
MODULE_NAMES = {m["id"]: m["name"] for m in MODULES}

# The scenario runner switches modules for one run without touching the saved toggles.
_override: ContextVar[Optional[dict[str, bool]]] = ContextVar("module_override", default=None)


def is_on(module_id: str) -> bool:
    forced = _override.get()
    if forced is not None and module_id in forced:
        return forced[module_id]
    return db.get_setting(f"module.{module_id}", "1") == "1"


def set_on(module_id: str, on: bool) -> None:
    db.set_setting(f"module.{module_id}", "1" if on else "0")


def states() -> dict[str, bool]:
    return {module_id: is_on(module_id) for module_id in MODULE_IDS}


@contextmanager
def override(forced: dict[str, bool]) -> Iterator[None]:
    token = _override.set(forced)
    try:
        yield
    finally:
        _override.reset(token)


def load() -> None:
    """Import every module so it can subscribe to events."""
    for name in ("shared_classes", "alert_meaning", "logic_text", "design_hints",
                 "tag_suggest", "template_pick", "quiz_scoring"):
        importlib.import_module(f"sogo_lite.modules.{name}")
