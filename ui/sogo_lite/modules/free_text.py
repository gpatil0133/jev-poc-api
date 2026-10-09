"""Feature concept: turn free text into a category at submit.

The author attaches a category list to a Text Box such as "What is your job
title?". Each typed answer is matched to one category when the response is
submitted and stored next to the typed answer. Counts (what a quota or a segment
would read) use confident matches only: "can't tell" and unsure answers never count.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import db
from sogo_lite.modules import inferred

MODULE = "free_text_category"
SPEC_ID = "category"
CANT_TELL = "cant_tell"
MAX_CATEGORIES = 30


def get(survey_no: int, qid: str) -> Optional[dict[str, Any]]:
    found = db.one("SELECT * FROM category_list WHERE survey_no=? AND qid=?", survey_no, qid)
    if found:
        found["classes"] = db.loads(found["classes"], {})
    return found


def has_list(survey_no: int, qid: str) -> bool:
    """A Text Box with a category list holds a short typed value, not a comment."""
    return bool(db.val("SELECT 1 FROM category_list WHERE survey_no=? AND qid=?", survey_no, qid))


def parse_lines(text: str) -> dict[str, str]:
    """`label: description` per line; the description is optional."""
    classes: dict[str, str] = {}
    for line in (text or "").splitlines():
        label, _, description = line.partition(":")
        if label.strip():
            classes[label.strip()] = description.strip()
    return classes


def save(survey_no: int, qid: str, instructions: str, lines: str) -> tuple[bool, str]:
    classes = parse_lines(lines)
    classes.pop(CANT_TELL, None)
    if not classes:
        db.run("DELETE FROM category_list WHERE survey_no=? AND qid=?", survey_no, qid)
        return True, "Category list removed."
    if len(classes) < 2:
        return False, "A category list needs at least two categories."
    if len(classes) > MAX_CATEGORIES:
        return False, f"A category list holds at most {MAX_CATEGORIES} categories."
    db.run("INSERT INTO category_list(survey_no, qid, instructions, classes) VALUES(?,?,?,?)"
           " ON CONFLICT(survey_no, qid) DO UPDATE SET instructions=excluded.instructions, classes=excluded.classes",
           survey_no, qid, instructions.strip()[:400] or None, db.dumps(classes))
    return True, "Category list saved. Answers are matched when a response is submitted."


def spec(question: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    return {"id": SPEC_ID, "type": "choice",
            "instructions": found["instructions"] or "Which category does this typed answer belong to?",
            "classes": {**found["classes"], CANT_TELL: "the answer does not show a category, or is not a real answer"}}


def _specs(resp: dict[str, Any], question: dict[str, Any]) -> list[dict[str, Any]]:
    found = get(resp["survey_no"], question["qid"])
    return [spec(question, found)] if found else []


def counts(survey_no: int, question: dict[str, Any]) -> Optional[dict[str, Any]]:
    """What a quota on this question would read. Only confident matches are counted."""
    found = get(survey_no, question["qid"])
    if not found:
        return None
    wanted = spec(question, found)
    out: dict[str, Any] = {"counted": {label: 0 for label in found["classes"]}, "cant_tell": 0, "unsure": 0}
    for answers in inferred.by_response(survey_no, question["qid"]).values():
        answer = answers.get(SPEC_ID)
        if not answer or answer.get("_spec") != inferred.spec_hash(wanted):
            continue
        label = inferred.counted_label(answer)
        if label in out["counted"]:
            out["counted"][label] += 1
        elif inferred.confident(answer):
            out["cant_tell"] += 1
        else:
            out["unsure"] += 1
    typed = db.val("SELECT COUNT(*) FROM response_answer a JOIN response r ON r.id=a.response_id"
                   " WHERE r.survey_no=? AND a.qid=? AND a.text IS NOT NULL AND r.submitted_at IS NOT NULL",
                   survey_no, question["qid"]) or 0
    out["not_matched_yet"] = max(0, typed - sum(out["counted"].values()) - out["cant_tell"] - out["unsure"])
    return out


inferred.register(MODULE, _specs)
