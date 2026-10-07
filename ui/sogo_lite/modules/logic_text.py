"""Scope 3: Logic on text answers (plan section 8.3).

Adds the operator "answer is about…". The page's text answers are classified at
`page.next` with the respondent-facing timeout; the condition itself only reads the
stored result, so a timeout, an error, low confidence or Offline Mode all end on
the default path with nothing shown to the participant.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import db, engine, events, gateway
from sogo_lite.modules import shared_classes

MODULE = "logic_text"
OP = "about"
OP_LABEL = "answer is about…"


def add_rule(survey_no: int, source_qid: str, source: str, value: str, description: str,
             target: str, min_prob: Optional[float]) -> tuple[bool, str]:
    """Create an "answer is about…" rule. A typed topic is added to the question's
    class set as a yes/no question. The only action offered is showing a question:
    a meaning condition shows a follow-up, it never skips one."""
    value = (value or "").strip()
    if not value:
        return False, "Type a topic or pick a class."
    if source == "class":
        ref = value
    else:
        about = f"{value}: {description.strip()}" if description.strip() else value
        spec_id = f"about_{shared_classes.slug(value)}"
        ok, message = shared_classes.ensure_yesno(
            survey_no, source_qid, spec_id, f"Is this survey answer about {value}?",
            f"the answer is about {about}", f"the answer is not about {value}")
        if not ok:
            return False, message
        ref = f"{spec_id}|yes"
    db.run("INSERT INTO logic_rule(survey_no, source_qid, op, operand, action, target, min_prob)"
           " VALUES(?,?,?,?,?,?,?)", survey_no, source_qid, OP, ref, "show", target, min_prob)
    return True, "Rule added."


def _on_page_next(response: dict[str, Any], questions: list[dict[str, Any]],
                  answers: dict[str, engine.Answer], **_: Any) -> None:
    if response["offline"]:
        return      # Offline Mode: no gateway call at all, the default path is taken.
    rules = [r for r in engine.logic_rules(response["survey_no"]) if r["op"] == OP]
    for q in questions:
        mine = [r for r in rules if r["source_qid"] == q["qid"]]
        text = (answers.get(q["qid"]) or {}).get("text") or ""
        if q["type"] != "text" or not mine or not text.strip():
            continue
        result = shared_classes.classify_answer(response, q["qid"], text, kind="respondent",
                                                trigger="page.next", module=MODULE)
        if result.answers is None:
            engine.add_note(response["id"], {"kind": "default path", "what": f"logic on {q['qid']}",
                                             "reason": result.reason})
            gateway.set_outcome(result.log_id, "default path")
            continue
        shown = [r["target"] for r in mine
                 if shared_classes.meets(result.answers, r["operand"], r["min_prob"])[0]]
        gateway.set_outcome(result.log_id, f"branch taken: show {', '.join(shown)}" if shown
                            else "default path (no condition met)")


def _about(rule: dict[str, Any], ctx: engine.Context) -> bool:
    row = shared_classes.stored(ctx["response"]["id"], ctx["survey_no"], rule["source_qid"])
    if row is None:
        return False
    return shared_classes.meets(row["answers"], rule["operand"], rule["min_prob"])[0]


engine.register_logic_op(OP, MODULE, _about)
events.subscribe("page.next", MODULE, _on_page_next)
