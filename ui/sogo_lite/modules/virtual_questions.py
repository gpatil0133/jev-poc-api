"""Feature concept: Virtual Questions.

A closed question nobody was asked: the author writes the question and its
options, and each respondent's answer is read from the comment they already wrote.

  preview        10 comments, nothing stored, so the author can reword first
  run            a random sample (about 400 gives the share to within 5 points) or every comment
  going forward  new responses are answered at submit, with the other inferred answers
  use            as a Rules & Alerts condition, and as "say it once": skip a question
                 the comment already answered

Guardrails: an inferred answer is always shown as inferred; an answer the gateway
is unsure of counts as "not mentioned"; a question is skipped on band `act` only.
"""
from __future__ import annotations

import math
import random
from typing import Any, Optional

from sogo_lite import db, engine, events, gateway
from sogo_lite.modules import free_text, inferred

MODULE = "virtual_questions"
CONDITION = "virtual"
SKIP_OP = "vq_said"
SKIP_ACTION = "skip"
NOT_MENTIONED = "not_mentioned"
PREVIEW_SIZE = 10
SAMPLE_SIZE = 400
BATCH_SIZE = 64
MAX_OPTIONS = 12
DEFAULT_MIN_PROB = 0.80


def spec_id(vq_id: int) -> str:
    return f"vq_{vq_id}"


def _load(vq: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if vq:
        vq["classes"] = db.loads(vq["classes"], {})
    return vq


def get(vq_id: int) -> Optional[dict[str, Any]]:
    return _load(db.one("SELECT * FROM virtual_question WHERE id=?", vq_id))


def for_project(survey_no: int) -> list[dict[str, Any]]:
    return [_load(vq) for vq in db.rows("SELECT * FROM virtual_question WHERE survey_no=? ORDER BY id", survey_no)]


def spec(vq: dict[str, Any]) -> dict[str, Any]:
    return {"id": spec_id(vq["id"]), "type": "choice", "instructions": vq["wording"][:400], "classes": vq["classes"]}


def add(survey_no: int, source_qid: str, wording: str, lines: str, going_forward: bool = True) -> tuple[bool, str]:
    question = engine.question_by_qid(survey_no, source_qid)
    classes = free_text.parse_lines(lines)
    if not question or question["type"] != "text":
        return False, "Pick the Text Box whose answers the question is read from."
    if not wording.strip() or not [k for k in classes if k != NOT_MENTIONED]:
        return False, "Give the question some wording and at least one answer option."
    if len(classes) > MAX_OPTIONS:
        return False, f"A Virtual Question has at most {MAX_OPTIONS} options."
    classes.setdefault(NOT_MENTIONED, "does not talk about this")
    draft = {"id": "vq_draft", "type": "choice", "instructions": wording.strip()[:400], "classes": classes}
    check = gateway.call("POST", "/v1/tasks/compile", {"questions": [draft]}, module=MODULE,
                         trigger="Virtual Question saved", kind="author", survey_no=survey_no)
    lines_over = [l["id"] for l in ((check.data.get("budget") or {}).get("lines") or []) if l["status"] == "over"]
    if check.status == 422 or lines_over:
        gateway.set_outcome(check.log_id, "refused: the options do not fit")
        return False, ("The options do not fit the model's budget. Shorten the descriptions or use fewer options. "
                       + "; ".join(check.detail.get("errors") or []))
    gateway.set_outcome(check.log_id, "budget ok" if not check.fallback else "budget not checked (fallback)")
    db.run("INSERT INTO virtual_question(survey_no, source_qid, wording, classes, going_forward, created_at)"
           " VALUES(?,?,?,?,?,?)", survey_no, source_qid, wording.strip(), db.dumps(classes),
           1 if going_forward else 0, db.now())
    return True, "Virtual Question saved." + (" The gateway could not check its budget." if check.fallback else "")


def delete(vq_id: int) -> None:
    db.run("DELETE FROM logic_rule WHERE op=? AND operand=?", SKIP_OP, str(vq_id))
    db.run("DELETE FROM virtual_question WHERE id=?", vq_id)


def effective(answer: Optional[dict[str, Any]]) -> Optional[str]:
    """The answer the product reads. Unsure lands on "not mentioned"."""
    if not answer:
        return None
    if answer.get("_forced") or answer.get("band") not in ("act", "suggest") or answer.get("label") == "other":
        return NOT_MENTIONED
    return answer.get("label")


# ── answering ───────────────────────────────────────────────────────────────

def _specs(resp: dict[str, Any], question: dict[str, Any]) -> list[dict[str, Any]]:
    return [spec(vq) for vq in for_project(resp["survey_no"])
            if vq["source_qid"] == question["qid"] and vq["going_forward"]]


def _comments(vq: dict[str, Any]) -> list[dict[str, Any]]:
    """Submitted responses with a comment on the source question, oldest first."""
    return db.rows("SELECT r.id AS response_id, a.text FROM response r JOIN response_answer a ON a.response_id=r.id"
                   " WHERE r.survey_no=? AND a.qid=? AND a.text IS NOT NULL AND r.submitted_at IS NOT NULL"
                   " ORDER BY r.id", vq["survey_no"], vq["source_qid"])


def _batch(vq: dict[str, Any], comments: list[dict[str, Any]], trigger: str) -> gateway.GatewayResult:
    question = engine.question_by_qid(vq["survey_no"], vq["source_qid"])
    items = [{"id": str(c["response_id"]), **inferred.item(c["response_id"], question, c["text"])} for c in comments]
    return gateway.call("POST", "/v1/classify/batch", {"questions": [spec(vq)], "items": items}, module=MODULE,
                        trigger=trigger, kind="batch", survey_no=vq["survey_no"])


def preview(vq: dict[str, Any], size: int = PREVIEW_SIZE) -> tuple[list[dict[str, Any]], str]:
    """Answer a few comments without storing anything."""
    comments = _comments(vq)
    picked = random.Random(vq["id"]).sample(comments, min(size, len(comments)))
    if not picked:
        return [], "There are no comments on that question yet."
    result = _batch(vq, picked, "Virtual Question preview")
    if result.fallback:
        gateway.set_outcome(result.log_id, "no preview (fallback)")
        return [], f"No preview: the gateway did not answer ({result.message})."
    by_id = {r.get("id"): (r.get("answers") or {}).get(spec_id(vq["id"])) for r in result.data.get("results") or []}
    gateway.set_outcome(result.log_id, f"preview of {len(picked)} comment(s); nothing stored")
    return [{**c, "answer": by_id.get(str(c["response_id"])),
             "effective": effective(by_id.get(str(c["response_id"])))} for c in picked], ""


def run(vq: dict[str, Any], limit: Optional[int] = SAMPLE_SIZE) -> dict[str, Any]:
    """Answer comments that have no current answer: a random sample of `limit`, or all of them."""
    wanted = spec(vq)
    todo = [c for c in _comments(vq) if inferred.answer(c["response_id"], vq["source_qid"], wanted) is None]
    answered = len(_comments(vq)) - len(todo)
    if limit is not None:
        todo = random.Random(vq["id"]).sample(todo, max(0, min(limit - answered, len(todo))))
    out = {"sent": len(todo), "stored": 0, "failed": 0, "unsure": 0}
    for start in range(0, len(todo), BATCH_SIZE):
        chunk = todo[start:start + BATCH_SIZE]
        result = _batch(vq, chunk, "Virtual Question run")
        forced = "forced_low_confidence" in result.faults
        if result.fallback or forced:
            # Nothing unsure is kept: the comments stay unanswered and can be run again.
            out["failed" if result.fallback else "unsure"] += len(chunk)
            gateway.set_outcome(result.log_id, "nothing stored" + (" (fallback)" if result.fallback else " (unsure)"))
            if result.fallback:
                break
            continue
        for row in result.data.get("results") or []:
            answer = (row.get("answers") or {}).get(wanted["id"])
            if answer and str(row.get("id") or "").isdigit():
                inferred.save(int(row["id"]), vq["source_qid"], wanted, answer, result.log_id)
                out["stored"] += 1
        gateway.set_outcome(result.log_id, f"{len(chunk)} inferred answer(s) stored")
    return out


def summary(vq: dict[str, Any]) -> dict[str, Any]:
    """Shares over the answered comments, with the sampling margin when not all are answered."""
    wanted = spec(vq)
    total = len(_comments(vq))
    tally = {label: 0 for label in vq["classes"]}
    unsure = 0
    for answers in inferred.by_response(vq["survey_no"], vq["source_qid"]).values():
        answer = answers.get(wanted["id"])
        if not answer or answer.get("_spec") != inferred.spec_hash(wanted):
            continue
        label = effective(answer)
        key = label if label in tally else NOT_MENTIONED
        tally[key] = tally.get(key, 0) + 1
        unsure += 0 if inferred.confident(answer) else 1
    n = sum(tally.values())
    margin = None
    if n and n < total:
        # Worst case (a 50% share) at 95% confidence, corrected for the size of the survey.
        margin = round(100.0 * 1.96 * math.sqrt(0.25 / n) * math.sqrt((total - n) / max(1, total - 1)), 1)
    return {"total": total, "answered": n, "unsure": unsure, "margin": margin,
            "shares": [{"label": label, "count": count, "pct": round(100.0 * count / n, 1) if n else None}
                       for label, count in tally.items()]}


def answers_for(response_id: int, survey_no: int) -> list[dict[str, Any]]:
    """The inferred answers of one response, for Individual Responses."""
    out = []
    for vq in for_project(survey_no):
        answer = inferred.answer(response_id, vq["source_qid"], spec(vq))
        if answer:
            out.append({"vq": vq, "answer": answer, "effective": effective(answer)})
    return out


# ── use: Rules & Alerts condition ───────────────────────────────────────────

def build_condition(vq: dict[str, Any], label: str, min_prob: Optional[float]) -> tuple[Optional[dict[str, Any]], str]:
    if label not in vq["classes"]:
        return None, "Pick one of the question's options."
    return {"type": CONDITION, "vq_id": vq["id"], "qid": vq["source_qid"], "answer": label, "min_prob": min_prob,
            "label": f"Virtual Question \"{vq['wording']}\" is \"{label}\" (inferred"
                     + (f", min {min_prob:.2f})" if min_prob is not None else ", band act)")}, ""


def _evaluate(cond: dict[str, Any], ctx: engine.Context) -> tuple[bool, str]:
    vq = get(cond["vq_id"])
    question = engine.question_by_qid(ctx["survey_no"], cond["qid"]) if vq else None
    text = (ctx["answers"].get(cond["qid"]) or {}).get("text") or ""
    if not vq or not question or not text.strip():
        return False, "no comment" if vq else "the Virtual Question was deleted"
    answers = inferred.for_response(ctx["response"], question, text, kind="submit", trigger="response.submitted")
    answer = (answers or {}).get(spec_id(vq["id"]))
    if not answer or answer.get("_forced"):
        return False, "no inferred answer"
    probability = float((answer.get("probabilities") or {}).get(cond["answer"], 0.0))
    if cond.get("min_prob") is not None:
        return probability >= cond["min_prob"], f"inferred {probability:.2f}"
    return inferred.confident(answer) and answer.get("label") == cond["answer"], f"inferred {probability:.2f}"


# ── use: say it once ────────────────────────────────────────────────────────

def add_skip(vq: dict[str, Any], target_qid: str) -> tuple[bool, str]:
    source = engine.question_by_qid(vq["survey_no"], vq["source_qid"])
    target = engine.question_by_qid(vq["survey_no"], target_qid)
    if not source or not target or target["page_ord"] <= source["page_ord"]:
        return False, "Pick a question on a later page than the comment."
    if target["required"] == "mandatory":
        return False, "A Mandatory question is always asked."
    # Skipping needs the answer while the survey is being taken.
    db.run("UPDATE virtual_question SET going_forward=1 WHERE id=?", vq["id"])
    db.run("INSERT INTO logic_rule(survey_no, source_qid, op, operand, action, target) VALUES(?,?,?,?,?,?)",
           vq["survey_no"], vq["source_qid"], SKIP_OP, str(vq["id"]), SKIP_ACTION, target_qid)
    return True, f"{target_qid} is skipped when the comment already answers it."


def _said(rule: dict[str, Any], ctx: engine.Context) -> bool:
    """Met only when the stored answer is confident and names an option: a question
    is never skipped on a timeout, an error or an unsure answer."""
    vq = get(int(rule["operand"])) if str(rule["operand"]).isdigit() else None
    if not vq:
        return False
    answer = inferred.answer(ctx["response"]["id"], vq["source_qid"], spec(vq))
    return inferred.counted_label(answer) is not None


def _on_page_next(response: dict[str, Any], questions: list[dict[str, Any]],
                  answers: dict[str, engine.Answer], **_: Any) -> None:
    sources = {r["source_qid"] for r in engine.logic_rules(response["survey_no"]) if r["op"] == SKIP_OP}
    for q in questions:
        text = (answers.get(q["qid"]) or {}).get("text") or ""
        if q["qid"] in sources and q["type"] == "text" and text.strip():
            inferred.for_response(response, q, text, kind="respondent", trigger="page.next")


inferred.register(MODULE, _specs)
engine.register_alert_condition(CONDITION, MODULE, _evaluate)
engine.register_logic_op(SKIP_OP, MODULE, _said)
events.subscribe("page.next", MODULE, _on_page_next)
