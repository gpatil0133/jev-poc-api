"""Scope 8: open-ended quiz scoring (plan section 8.8).

When a grader opens the grading list, the ungraded answers go to the gateway in
one batch with two inline questions: which key points are present (`labels`), and
an overall grade (`choice`). Each suggestion is written to the Post-Population
fields as a draft. Only a grade the grader confirms counts as final.
"""
from __future__ import annotations

from typing import Any, Optional

from sogo_lite import db, engine, events, gateway
from sogo_lite.modules import shared_classes

MODULE = "quiz_scoring"
BATCH_MAX_ITEMS = 256
GRADE_LABELS = {"correct": "Correct", "partly_correct": "Partly correct", "incorrect": "Incorrect",
                "not_an_answer": "Not a real answer"}


def key_points(survey_no: int, qid: str) -> Optional[dict[str, Any]]:
    row = db.one("SELECT * FROM key_point_set WHERE survey_no=? AND qid=?", survey_no, qid)
    if row:
        row["key_points"] = db.loads(row["key_points"], [])
    return row


def save_key_points(survey_no: int, qid: str, model_answer: str, points: list[str]) -> None:
    db.run("INSERT INTO key_point_set(survey_no, qid, model_answer, key_points) VALUES(?,?,?,?)"
           " ON CONFLICT(survey_no, qid) DO UPDATE SET model_answer=excluded.model_answer,"
           " key_points=excluded.key_points", survey_no, qid, model_answer.strip(),
           db.dumps(list(dict.fromkeys(p.strip() for p in points if p.strip()))))


def specs(points: list[str], model_answer: str = "") -> list[dict[str, Any]]:
    expected = "; ".join(points) or model_answer
    return [
        {"id": "kp", "type": "labels", "classes": {point: "" for point in points},
         "instructions": "Does this quiz answer state this point?"},
        {"id": "grade", "type": "choice",
         "instructions": "How well does this quiz answer match the expected answer?",
         "classes": {
             "correct": f"covers every expected point: {expected}",
             "partly_correct": f"covers some but not all of the expected points: {expected}",
             "incorrect": "a real attempt that covers none of the expected points or gets them wrong",
             "not_an_answer": "blank, off-topic, a joke or declines to answer",
         }},
    ]


def ungraded(survey_no: int, qid: str) -> list[dict[str, Any]]:
    return db.rows(
        "SELECT r.id AS response_id, a.text FROM response r JOIN response_answer a ON a.response_id=r.id"
        " WHERE r.survey_no=? AND a.qid=? AND r.submitted_at IS NOT NULL AND TRIM(COALESCE(a.text,''))<>''"
        " AND NOT EXISTS (SELECT 1 FROM post_populate p WHERE p.response_id=r.id AND p.qid=a.qid)"
        " ORDER BY r.id", survey_no, qid)


def _on_grading_opened(survey_no: int, question: dict[str, Any], **_: Any) -> None:
    kps = key_points(survey_no, question["qid"])
    points = (kps or {}).get("key_points") or []
    pending = ungraded(survey_no, question["qid"])
    if not points or not pending:
        return
    questions = specs(points, kps.get("model_answer") or "")
    for start in range(0, len(pending), BATCH_MAX_ITEMS):
        chunk = pending[start:start + BATCH_MAX_ITEMS]
        body = {"questions": questions,
                "items": [{"id": str(row["response_id"]), "text": row["text"],
                           "survey_question": question["wording"][:1000]} for row in chunk]}
        result = gateway.call("POST", "/v1/classify/batch", body, module=MODULE, trigger="grading.opened",
                              kind="batch", survey_no=survey_no)
        if result.fallback:
            gateway.set_outcome(result.log_id, "no suggestions; the grader grades by hand")
            continue
        written = 0
        for item in result.data.get("results") or []:
            written += _write_draft(int(item["id"]), question, points, item.get("answers") or {})
        gateway.set_outcome(result.log_id, f"{written} draft grades suggested for {len(chunk)} answers")


def _write_draft(response_id: int, question: dict[str, Any], points: list[str],
                 answers: dict[str, dict[str, Any]]) -> int:
    grade = answers.get("grade") or {}
    if grade.get("_forced") or grade.get("label") is None:
        return 0        # the model is unsure: the row shows the answer with no suggestion
    found = [p for p in points
             if (answers.get(f"kp.{shared_classes.label_slug(p)}") or {}).get("label") == "yes"]
    label = grade["label"]
    max_points = question["max_points"] or float(len(points))
    # A grade the gateway bands `none` is listed with its evidence but no suggested score.
    score = round(max_points * len(found) / len(points) * 2) / 2 if grade.get("band") != "none" else None
    engine.save_post_populate(
        response_id, question["qid"], score,
        f"Suggested: {GRADE_LABELS.get(label, label)} ({len(found)} of {len(points)} key points)",
        status="draft", source="suggestion",
        detail={"grade": label, "confidence": grade.get("confidence"), "band": grade.get("band"),
                "found": found, "total": len(points)})
    return 1


def grading_rows(survey_no: int, qid: str) -> list[dict[str, Any]]:
    """Every submitted answer to the question, lowest confidence first so the
    grader's attention goes where the suggestion is weakest."""
    found = db.rows(
        "SELECT r.id AS response_id, a.text, p.score, p.feedback, p.status, p.source, p.detail"
        " FROM response r JOIN response_answer a ON a.response_id=r.id"
        " LEFT JOIN post_populate p ON p.response_id=r.id AND p.qid=a.qid"
        " WHERE r.survey_no=? AND a.qid=? AND r.submitted_at IS NOT NULL AND TRIM(COALESCE(a.text,''))<>''",
        survey_no, qid)
    for row in found:
        row["detail"] = db.loads(row["detail"], {})
    found.sort(key=lambda r: (r["status"] == "confirmed", r["detail"].get("confidence", -1.0), r["response_id"]))
    return found


# ── jobs polling test (kept apart from the default path, see plan 8.8) ───────

def start_job(survey_no: int, qid: str, total: int) -> gateway.GatewayResult:
    """Send `total` answers to POST /v1/jobs by cycling the question's real answers.
    The gateway writes job output to a file on its own host and has no endpoint that
    returns it, so this only tests progress polling."""
    question = engine.question_by_qid(survey_no, qid)
    kps = key_points(survey_no, qid) or {"key_points": [], "model_answer": ""}
    texts = [r["text"] for r in db.rows(
        "SELECT a.text FROM response_answer a JOIN response r ON r.id=a.response_id"
        " WHERE r.survey_no=? AND a.qid=? AND TRIM(COALESCE(a.text,''))<>''", survey_no, qid)] or ["no answer"]
    items = [{"id": f"job-{i}", "text": f"{texts[i % len(texts)]} ({i})",
              "survey_question": question["wording"][:1000]} for i in range(total)]
    result = gateway.call("POST", "/v1/jobs", {"questions": specs(kps["key_points"], kps["model_answer"] or ""),
                                               "items": items},
                          module=MODULE, trigger="jobs polling test", kind="batch", survey_no=survey_no)
    gateway.set_outcome(result.log_id, "job not started" if result.fallback
                        else f"job {result.data.get('job_id')} accepted")
    return result


def job_status(job_id: str) -> gateway.GatewayResult:
    result = gateway.call("GET", f"/v1/jobs/{job_id}", module=MODULE, trigger="jobs polling test", kind="author")
    if not result.fallback:
        gateway.set_outcome(result.log_id, f"{result.data.get('status')}: {result.data.get('processed')}"
                                           f"/{result.data.get('total')}")
    return result


events.subscribe("grading.opened", MODULE, _on_grading_opened)
