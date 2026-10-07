"""Scope 2: Rules & Alerts on comment meaning (plan section 8.2).

Adds the condition type "the comment means…". It is evaluated at
`response.submitted`, after the rule's existing conditions, from the stored
classification or a fresh one with the submit-time timeout.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import engine, gateway
from sogo_lite.modules import shared_classes

MODULE = "alert_meaning"
CONDITION = "meaning"
DEFAULT_MIN_PROB = 0.80


def ready_made() -> list[dict[str, Any]]:
    """Ready-made meanings: the catalog's yes/no tasks on a text answer."""
    return [t for t in shared_classes.comment_tasks() if t["type"] == "yesno"]


def build_condition(survey_no: int, qid: str, source: str, value: str,
                    min_prob: Optional[float]) -> tuple[Optional[dict[str, Any]], str]:
    """Turn the rule editor's choice into a stored condition. A ready-made or written
    meaning is first added to the question's class set. Returns (condition, error)."""
    value = (value or "").strip()
    if not value:
        return None, "Pick a class or meaning, or write one."
    if source == "class":
        ref, label = value, value.replace("|", " is ")
    elif source == "ready":
        ok, message = shared_classes.ensure_task(survey_no, qid, value)
        if not ok:
            return None, message
        ref, label = f"{value}|yes", value
    else:
        spec_id = f"means_{shared_classes.slug(value)}"
        ok, message = shared_classes.ensure_yesno(
            survey_no, qid, spec_id, f"Does this survey answer say or imply this: {value}?",
            value, "the answer does not say or imply this")
        if not ok:
            return None, message
        ref, label = f"{spec_id}|yes", value
    return {"type": CONDITION, "qid": qid, "ref": ref, "min_prob": min_prob,
            "label": f"comment on {qid} means \"{label}\""
                     + (f" (min {min_prob:.2f})" if min_prob is not None else " (band act)")}, ""


def _evaluate(cond: dict[str, Any], ctx: engine.Context) -> tuple[bool, str]:
    resp = ctx["response"]
    text = (ctx["answers"].get(cond["qid"]) or {}).get("text") or ""
    if not text.strip():
        return False, "no comment"
    # One attempt per question per submit: several rules on a comment must not each
    # wait out the timeout when the gateway cannot answer.
    failed: dict[str, shared_classes.Classification] = ctx.setdefault("_no_result", {})
    result = failed.get(cond["qid"]) or shared_classes.classify_answer(
        resp, cond["qid"], text, kind="submit", trigger="response.submitted", module=MODULE)
    if result.answers is None:
        failed[cond["qid"]] = result
        # No result counts as not met, and the skipped check stays visible on the response.
        engine.add_note(resp["id"], {"kind": "check skipped", "what": cond.get("label", "meaning condition"),
                                     "reason": result.reason})
        return False, f"skipped: {result.reason}"
    met, probability = shared_classes.meets(result.answers, cond["ref"], cond.get("min_prob"))
    gateway.set_outcome(result.log_id, f"alert condition {'met' if met else 'not met'}: "
                                       f"{cond['ref']} ({probability:.2f})")
    return met, f"matched {probability:.2f}"


engine.register_alert_condition(CONDITION, MODULE, _evaluate)
