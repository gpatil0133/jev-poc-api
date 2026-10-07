"""Accepting and dismissing classifier suggestions.

Nothing the classifier suggests is applied to tags or settings without an author
action; this is the only place a suggestion is applied.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import db
from sogo_lite.modules import design_hints, tag_suggest


def get(suggestion_id: int) -> Optional[dict[str, Any]]:
    return db.one("SELECT * FROM suggestion WHERE id=?", suggestion_id)


def accept(suggestion_id: int) -> bool:
    suggestion = get(suggestion_id)
    if not suggestion or suggestion["status"] != "pending":
        return False
    if suggestion["kind"] == "tag":
        tag_suggest.apply(suggestion)
    else:
        design_hints.apply(suggestion)
    db.run("UPDATE suggestion SET status='accepted' WHERE id=?", suggestion_id)
    return True


def dismiss(suggestion_id: int) -> None:
    db.run("UPDATE suggestion SET status='dismissed' WHERE id=? AND status='pending'", suggestion_id)
