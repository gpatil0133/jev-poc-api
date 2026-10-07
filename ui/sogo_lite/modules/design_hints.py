"""Scopes 4, 5 and 9: builder hints on `question.saved` (plan sections 8.4 and 8.5).

  4. sensitive question        -> suggest Encouraged Response instead of Mandatory
  5. Text Box                  -> suggest marking it as a "why" follow-up
  9. asks for personal data    -> suggest marking the question as personal data

All read the question wording only, and share one classify call when more than one
is on. Nothing is applied until the author accepts the suggestion.
"""
from __future__ import annotations

import hashlib
from typing import Any

from sogo_lite import db, engine, events, gateway, modules
from sogo_lite.modules import pii

SENSITIVE_MODULE = "sensitive_hint"
FOLLOWUP_MODULE = "followup_flag"
KIND_MODULES = {"sensitive": SENSITIVE_MODULE, "followup": FOLLOWUP_MODULE, "pii": pii.QUESTION_MODULE}
KINDS = tuple(KIND_MODULES)

SENSITIVE_SPEC: dict[str, Any] = {
    "id": "sensitive", "type": "yesno",
    "instructions": "Does this survey question ask for sensitive personal information?",
    "classes": {
        "true": "asks about income, finances, health, religion, sexuality, politics, criminal record "
                "or another private matter people may not want to disclose",
        "false": "an everyday question people answer without discomfort",
    },
}
FOLLOWUP_SPEC: dict[str, Any] = {
    "id": "followup", "type": "yesno",
    "instructions": "Does this survey question ask someone to explain the reason for a score or rating they gave?",
    "classes": {
        "true": "asks why, or for the main reason behind, a score or rating",
        "false": "asks for a fact, a detail or anything other than the reason for a score",
    },
}


def _signature(question: dict[str, Any], wanted: list[str]) -> str:
    raw = "|".join([question["wording"], question["type"], question["required"],
                    str(question["is_followup"]), question["pii_kind"] or "", ",".join(wanted)])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _previous(question: dict[str, Any]) -> list[dict[str, Any]]:
    """Questions before this one, nearest first."""
    before = []
    for q in engine.questions(question["survey_no"]):
        if q["id"] == question["id"]:
            break
        before.append(q)
    return before[::-1]


def _on_question_saved(question: dict[str, Any], **_: Any) -> None:
    wanted: list[tuple[str, dict[str, Any]]] = []
    if modules.is_on(SENSITIVE_MODULE):
        wanted.append((SENSITIVE_MODULE, SENSITIVE_SPEC))
    if modules.is_on(pii.QUESTION_MODULE):
        wanted.append((pii.QUESTION_MODULE, pii.QUESTION_SPEC))
    if modules.is_on(FOLLOWUP_MODULE) and question["type"] == "text" and not question["is_followup"]:
        wanted.append((FOLLOWUP_MODULE, FOLLOWUP_SPEC))
    if not wanted:
        return
    signature = _signature(question, [m for m, _ in wanted])
    if question["checked_sig"] == signature:
        return      # Saved again without changes: a dismissed hint stays dismissed.

    previous = _previous(question)
    body: dict[str, Any] = {"text": question["wording"], "questions": [spec for _, spec in wanted]}
    if previous:
        body["context"] = {"previous_question": previous[0]["wording"]}
    result = gateway.call("POST", "/v1/classify", body, module="+".join(m for m, _ in wanted),
                          trigger="question.saved", kind="author", survey_no=question["survey_no"])
    db.run("DELETE FROM suggestion WHERE target_type='question' AND target_id=? AND kind IN (?,?,?)",
           question["id"], *KINDS)
    if result.fallback:
        gateway.set_outcome(result.log_id, "no hint (fallback)")
        return
    if "forced_low_confidence" not in result.faults:
        db.run("UPDATE question SET checked_sig=? WHERE id=?", signature, question["id"])

    outcomes: list[str] = []
    sensitive = result.answers.get("sensitive")
    if sensitive and sensitive["band"] in ("act", "suggest"):
        # The mock's rule follows the scope page's only example: Mandatory -> Encouraged.
        if question["required"] == "mandatory":
            _suggest("sensitive", question, {"required": "encouraged"}, sensitive, signature)
            outcomes.append("sensitive hint created")
        else:
            outcomes.append("sensitive, but the setting already matches: no hint")
    kind = pii.found_kind(result.answers.get(pii.QUESTION_SPEC["id"]) or {})
    if kind and kind != question["pii_kind"]:
        _suggest("pii", question, {"pii_kind": kind}, result.answers[pii.QUESTION_SPEC["id"]], signature)
        outcomes.append(f"personal data hint created: {pii.KINDS[kind].lower()}")
    followup = result.answers.get("followup")
    if followup and followup["band"] in ("act", "suggest"):
        parent = next((q["qid"] for q in previous if q["type"] == "metric"), None)
        _suggest("followup", question, {"is_followup": 1, "parent_qid": parent}, followup, signature)
        outcomes.append("follow-up suggestion created")
    gateway.set_outcome(result.log_id, "; ".join(outcomes) or "no hint")


def _suggest(kind: str, question: dict[str, Any], proposed: dict[str, Any], answer: dict[str, Any],
             signature: str) -> None:
    db.run("INSERT INTO suggestion(kind, survey_no, target_type, target_id, signature, proposed, confidence,"
           " band, created_at) VALUES(?,?,?,?,?,?,?,?,?)", kind, question["survey_no"], "question",
           question["id"], signature, db.dumps(proposed),
           answer["value"] if answer.get("value") is not None else answer.get("confidence"),
           answer["band"], db.now())


def pending(question_id: int) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM suggestion WHERE target_type='question' AND target_id=? AND status='pending'"
                    " AND kind IN (?,?,?) ORDER BY id", question_id, *KINDS)
    for s in found:
        s["proposed"] = db.loads(s["proposed"], {})
    return [s for s in found if modules.is_on(KIND_MODULES[s["kind"]])]


def apply(suggestion: dict[str, Any]) -> None:
    proposed = db.loads(suggestion["proposed"], {})
    if suggestion["kind"] == "pii":
        db.run("UPDATE question SET pii_kind=? WHERE id=?", proposed["pii_kind"], suggestion["target_id"])
    elif suggestion["kind"] == "sensitive":
        db.run("UPDATE question SET required=? WHERE id=?", proposed["required"], suggestion["target_id"])
    elif suggestion["kind"] == "followup":
        db.run("UPDATE question SET is_followup=1, parent_qid=COALESCE(parent_qid, ?) WHERE id=?",
               proposed.get("parent_qid"), suggestion["target_id"])


events.subscribe("question.saved", None, _on_question_saved)
