"""Accepting and dismissing classifier suggestions.

Nothing the classifier suggests is applied to tags or settings without an author
action; this is the only place a suggestion is applied.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import db
from sogo_lite.modules import design_hints, survey_coach, tag_suggest

COACH_KINDS = tuple(survey_coach.KIND_MODULES)


def question_hints(question_id: int) -> list[dict[str, Any]]:
    """Every pending builder hint on a question: scopes 4, 5 and 9, then Survey Coach."""
    return design_hints.pending(question_id) + survey_coach.pending(question_id)


def get(suggestion_id: int) -> Optional[dict[str, Any]]:
    return db.one("SELECT * FROM suggestion WHERE id=?", suggestion_id)


def accept(suggestion_id: int) -> bool:
    suggestion = get(suggestion_id)
    if not suggestion or suggestion["status"] != "pending":
        return False
    if suggestion["kind"] == "tag":
        tag_suggest.apply(suggestion)
    elif suggestion["kind"] in COACH_KINDS:
        survey_coach.apply(suggestion)
    else:
        design_hints.apply(suggestion)
    db.run("UPDATE suggestion SET status='accepted' WHERE id=?", suggestion_id)
    return True


def dismiss(suggestion_id: int) -> None:
    db.run("UPDATE suggestion SET status='dismissed' WHERE id=? AND status='pending'", suggestion_id)
