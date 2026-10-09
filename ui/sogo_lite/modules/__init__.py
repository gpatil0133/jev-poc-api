"""The Design scopes as switchable modules (plan section 4.1): the eight from the
scope page, plus two for personal data (9 and 10).

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
    {"id": "pii_question", "n": "9", "name": "Builder hints for questions that ask for personal data",
     "where": "Design → question editor"},
    {"id": "pii_answer", "n": "10", "name": "Personal data in text answers",
     "where": "Responses → individual response"},
    # The feature concepts (docs/JEV-Feature-concepts.html). Callback offer has no
    # switch of its own: it is a ready-made meaning in the scope 3 rule editor.
    {"id": "survey_coach", "n": "F1", "name": "Survey Coach: question wording",
     "where": "Design → question editor"},
    {"id": "qtype_pick", "n": "F2", "name": "Question type pick",
     "where": "Design → question editor"},
    {"id": "free_text_category", "n": "F3", "name": "Free text to category",
     "where": "Design → Text Box editor, Responses"},
    {"id": "identity_warning", "n": "F4", "name": "\"This might identify you\" warning",
     "where": "Live survey page, Anonymous projects"},
    {"id": "virtual_questions", "n": "F5", "name": "Virtual Questions",
     "where": "Design → Virtual Questions, Responses"},
    {"id": "coach_blind_spot", "n": "F6", "name": "Survey Coach: what no question asks",
     "where": "Design → Coach"},
    {"id": "feedback_owners", "n": "F7", "name": "Feedback Owners",
     "where": "Owners"},
    {"id": "fix_tracker", "n": "F8", "name": "Fix Tracker",
     "where": "Fix Tracker"},
    {"id": "invite_hold", "n": "F9", "name": "Invitation hold",
     "where": "Distribute"},
    {"id": "invite_replies", "n": "F10", "name": "Invitation replies",
     "where": "Distribute"},
]
MODULE_IDS = [m["id"] for m in MODULES]
MODULE_NAMES = {m["id"]: m["name"] for m in MODULES}
# Off until someone switches them on in Settings. Shared classes is parked for now:
# scopes 2 and 3 keep working on the classes their own rule editors add.
DEFAULT_OFF = {"shared_classes"}

# The scenario runner switches modules for one run without touching the saved toggles.
_override: ContextVar[Optional[dict[str, bool]]] = ContextVar("module_override", default=None)


def is_on(module_id: str) -> bool:
    forced = _override.get()
    if forced is not None and module_id in forced:
        return forced[module_id]
    return db.get_setting(f"module.{module_id}", "0" if module_id in DEFAULT_OFF else "1") == "1"


def set_on(module_id: str, on: bool) -> None:
    db.set_setting(f"module.{module_id}", "1" if on else "0")


def states() -> dict[str, bool]:
    return {module_id: is_on(module_id) for module_id in MODULE_IDS}


@contextmanager
def override(forced: dict[str, bool]) -> Iterator[None]:
    """Nested overrides add to the one already in force."""
    token = _override.set({**(_override.get() or {}), **forced})
    try:
        yield
    finally:
        _override.reset(token)


def load() -> None:
    """Import every module so it can subscribe to events."""
    for name in ("shared_classes", "alert_meaning", "logic_text", "design_hints",
                 "tag_suggest", "template_pick", "quiz_scoring", "pii",
                 "inferred", "free_text", "virtual_questions", "owners", "fix_tracker",
                 "survey_coach", "identity", "distribution"):
        importlib.import_module(f"sogo_lite.modules.{name}")
