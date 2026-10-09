"""What the feature concepts read from a text answer, classified once per answer.

Virtual Questions, Feedback Owners, Fix Tracker and Free text to category each
ask their own question about a text answer. A module registers a provider that
returns the questions it wants for an answer; this file sends them together in
one classify call and stores each answer next to the response, tagged with the
hash of the question that produced it. A question that was added or reworded
later is asked on its own; the rest is not sent again.

An answer the gateway is unsure of is still stored with its band. Readers act on
band `act` only (`confident`), so nothing unsure is ever counted or routed.
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable, Optional

from sogo_lite import db, engine, events, gateway, modules

# The escape option the gateway adds to a choice, and the quiet answers of the features.
QUIET = {"other", "none", "not_mentioned", "cant_tell"}
BACKFILL_GIVE_UP = 3

Spec = dict[str, Any]
# specs(response, question) -> the inline questions wanted for this answer
SpecsFn = Callable[[dict[str, Any], dict[str, Any]], list[Spec]]
# on_answer(response, question, spec, answer) -> what was done, for the Inspector
AnswerFn = Callable[[dict[str, Any], dict[str, Any], Spec, dict[str, Any]], Optional[str]]

_providers: list[tuple[str, SpecsFn, Optional[AnswerFn]]] = []


def register(module_id: str, specs: SpecsFn, on_answer: Optional[AnswerFn] = None) -> None:
    _providers.append((module_id, specs, on_answer))


def spec_hash(spec: Spec) -> str:
    return hashlib.sha1(db.dumps([spec.get("type"), spec.get("instructions"), spec.get("classes")])
                        .encode("utf-8")).hexdigest()[:12]


def confident(answer: Optional[dict[str, Any]]) -> bool:
    return bool(answer) and answer.get("band") == "act" and not answer.get("_forced")


def counted_label(answer: Optional[dict[str, Any]]) -> Optional[str]:
    """The label a count, quota or route may use: confident and not a quiet answer."""
    if not confident(answer) or answer.get("label") in QUIET:
        return None
    return answer.get("label")


# ── storage ─────────────────────────────────────────────────────────────────

def stored(response_id: int, qid: str) -> dict[str, dict[str, Any]]:
    return db.loads(db.val("SELECT answers FROM inferred_result WHERE response_id=? AND qid=?",
                           response_id, qid), {})


def answer(response_id: int, qid: str, spec: Spec) -> Optional[dict[str, Any]]:
    """The stored answer to this question, only while the question is unchanged."""
    found = stored(response_id, qid).get(spec["id"])
    return found if found and found.get("_spec") == spec_hash(spec) else None


def save(response_id: int, qid: str, spec: Spec, result: dict[str, Any], log_id: Optional[int]) -> dict[str, Any]:
    kept = {k: result.get(k) for k in ("label", "confidence", "band", "probabilities")}
    kept.update(_spec=spec_hash(spec), _log=log_id)
    answers = stored(response_id, qid)
    answers[spec["id"]] = kept
    db.run("INSERT INTO inferred_result(response_id, qid, answers, updated_at) VALUES(?,?,?,?)"
           " ON CONFLICT(response_id, qid) DO UPDATE SET answers=excluded.answers, updated_at=excluded.updated_at",
           response_id, qid, db.dumps(answers), db.now())
    return kept


def results_for(response_id: int) -> list[dict[str, Any]]:
    found = db.rows("SELECT * FROM inferred_result WHERE response_id=? ORDER BY qid", response_id)
    for row in found:
        row["answers"] = db.loads(row["answers"], {})
    return found


def by_response(survey_no: int, qid: str) -> dict[int, dict[str, dict[str, Any]]]:
    """Every stored answer set for one question of a project, by response id."""
    return {row["response_id"]: db.loads(row["answers"], {}) for row in db.rows(
        "SELECT i.response_id, i.answers FROM inferred_result i JOIN response r ON r.id=i.response_id"
        " WHERE r.survey_no=? AND i.qid=?", survey_no, qid)}


# ── classify once ───────────────────────────────────────────────────────────

def metric_score(response_id: int, question: dict[str, Any]) -> Optional[float]:
    """The score of the metric question a Text Box follows, which travels with the comment."""
    if not question.get("parent_qid"):
        return None
    return (engine.answers(response_id).get(question["parent_qid"]) or {}).get("number")


def item(response_id: int, question: dict[str, Any], text: str) -> dict[str, Any]:
    """The state fields of one text answer, as a classify body or a batch item."""
    body: dict[str, Any] = {"text": text, "survey_question": question["wording"][:1000]}
    score = metric_score(response_id, question)
    if score is not None:
        body["metric_score"] = score
    return body


def wanted(resp: dict[str, Any], question: dict[str, Any]) -> list[tuple[str, Spec, Optional[AnswerFn]]]:
    # In the order of the module list, whatever order the modules were imported in.
    ordered = sorted(_providers, key=lambda p: modules.MODULE_IDS.index(p[0]))
    return [(module_id, spec, on_answer) for module_id, specs, on_answer in ordered
            if modules.is_on(module_id) for spec in specs(resp, question)]


def for_response(resp: dict[str, Any], question: dict[str, Any], text: str, *, kind: str,
                 trigger: str) -> Optional[dict[str, dict[str, Any]]]:
    """The answers to every question wanted for this text answer, asking the gateway
    only for those not stored yet. None when the gateway could not answer."""
    if resp["offline"] or not (text or "").strip():
        return {}
    want = wanted(resp, question)
    if not want:
        return {}
    have = stored(resp["id"], question["qid"])
    need = [w for w in want if (have.get(w[1]["id"]) or {}).get("_spec") != spec_hash(w[1])]
    if need:
        body = {**item(resp["id"], question, text), "questions": [spec for _, spec, _ in need]}
        result = gateway.call("POST", "/v1/classify", body, module="+".join(dict.fromkeys(m for m, _, _ in need)),
                              trigger=trigger, kind=kind, survey_no=resp["survey_no"], response_id=resp["id"])
        if result.fallback:
            engine.add_note(resp["id"], {"kind": "check skipped", "what": f"inferred answers on {question['qid']}",
                                         "reason": result.reason})
            gateway.set_outcome(result.log_id, "nothing stored (fallback)")
            return None
        forced = "forced_low_confidence" in result.faults
        outcomes: list[str] = []
        for _, spec, on_answer in need:
            got = result.answers.get(spec["id"])
            if not got:
                continue
            if forced:
                # Forced-low-confidence fault: treated as unsure and not kept, so a later real answer is.
                have[spec["id"]] = {**got, "_spec": spec_hash(spec)}
                continue
            have[spec["id"]] = save(resp["id"], question["qid"], spec, got, result.log_id)
            done = on_answer(resp, question, spec, have[spec["id"]]) if on_answer else None
            outcomes.append(done or f"{spec['id']}: {got.get('label')} ({got.get('band')})")
        gateway.set_outcome(result.log_id, "; ".join(outcomes) or "unsure: nothing stored")
    return {spec["id"]: have[spec["id"]] for _, spec, _ in want if spec["id"] in have}


def _on_response_submitted(response: dict[str, Any], answers: dict[str, engine.Answer], **_: Any) -> None:
    for q in engine.questions(response["survey_no"]):
        text = (answers.get(q["qid"]) or {}).get("text") or ""
        if q["type"] == "text" and text.strip():
            for_response(response, q, text, kind="submit", trigger="response.submitted")


def backfill(survey_no: int) -> dict[str, int]:
    """Ask the wanted questions for responses that came in before a feature was set up,
    oldest first, so a contact's earlier complaint is known before their later answer."""
    counts = {"answers": 0, "failed": 0}
    questions = [q for q in engine.questions(survey_no) if q["type"] == "text"]
    failures = 0
    for resp in db.rows("SELECT * FROM response WHERE survey_no=? AND submitted_at IS NOT NULL AND offline=0"
                        " ORDER BY id", survey_no):
        given = engine.answers(resp["id"])
        for q in questions:
            text = (given.get(q["qid"]) or {}).get("text") or ""
            if not text.strip():
                continue
            if for_response(resp, q, text, kind="batch", trigger="backfill") is None:
                counts["failed"] += 1
                failures += 1
                if failures >= BACKFILL_GIVE_UP:
                    return counts
            else:
                counts["answers"] += 1
                failures = 0
    return counts


events.subscribe("response.submitted", None, _on_response_submitted)
