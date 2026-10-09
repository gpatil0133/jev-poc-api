"""Participate (the live survey page), Individual Responses, the grading list and
the three local sinks."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import FormData

from sogo_lite import db, engine, events, modules
from sogo_lite.modules import free_text, identity, inferred, pii, quiz_scoring, shared_classes, virtual_questions
from sogo_lite.web import form_data, project_or_404, redirect, render, to_float

router = APIRouter()


# ── Participate (5.8) ───────────────────────────────────────────────────────

@router.get("/take/{survey_no}", response_class=HTMLResponse)
def take_start(request: Request, survey_no: int) -> HTMLResponse:
    return render(request, "take_start.html", project=project_or_404(survey_no), tab="take")


@router.post("/take/{survey_no}")
def take_begin(survey_no: int, form: FormData = Depends(form_data)):
    proj = project_or_404(survey_no)
    respondent = None if proj["anonymous"] else (form.get("respondent") or "").strip() or None
    response_id = engine.start_response(survey_no, offline=bool(form.get("offline")), respondent=respondent)
    return redirect(f"/take/r/{response_id}")


def _response_or_404(response_id: int) -> dict[str, Any]:
    resp = engine.response(response_id)
    if not resp:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No response {response_id}.", "status": 404})
    return resp


def _page(request: Request, resp: dict[str, Any], provided: Optional[dict[str, engine.Answer]] = None,
          missing: Optional[list[str]] = None, encouraged: Optional[list[str]] = None,
          identity_notes: Optional[dict[str, str]] = None, reminded: bool = False) -> HTMLResponse:
    proj = engine.project(resp["survey_no"])
    if resp["submitted_at"]:
        return render(request, "take_done.html", project=proj, response=resp)
    questions = engine.current_questions(resp["id"])
    for q in questions:
        q["low"], q["high"] = engine.metric_range(q) if q["type"] == "metric" else (0, 0)
    return render(request, "take_page.html", project=proj, response=resp, questions=questions,
                  provided=provided or engine.answers(resp["id"]), missing=missing or [],
                  encouraged=encouraged or [], identity=identity_notes or {}, reminded=reminded,
                  pages=db.val("SELECT MAX(ord) FROM page WHERE survey_no=?", resp["survey_no"]))


@router.get("/take/r/{response_id}", response_class=HTMLResponse)
def take_page(request: Request, response_id: int) -> HTMLResponse:
    return _page(request, _response_or_404(response_id))


@router.post("/take/r/{response_id}", response_class=HTMLResponse)
def take_next(request: Request, response_id: int, background: BackgroundTasks,
              form: FormData = Depends(form_data)):
    resp = _response_or_404(response_id)
    if resp["submitted_at"]:
        return redirect(f"/take/r/{response_id}")
    visible = engine.current_questions(response_id)
    provided: dict[str, engine.Answer] = {}
    for q in visible:
        field = f"q_{q['qid']}"
        if q["type"] in ("radio", "checkbox"):
            ids = [int(v) for v in form.getlist(field) if str(v).isdigit()]
            provided[q["qid"]] = {"option_ids": ids, "text": None, "number": None}
        elif q["type"] == "metric":
            provided[q["qid"]] = {"option_ids": [], "text": None, "number": to_float(form.get(field))}
        else:
            provided[q["qid"]] = {"option_ids": [], "text": (form.get(field) or "").strip(), "number": None}
    missing = engine.missing_required(visible, provided)
    if missing["mandatory"]:
        return _page(request, resp, provided, missing=missing["mandatory"])
    if missing["encouraged"] and not form.get("reminded"):
        # Encouraged Response: ask once, then let the participant continue.
        return _page(request, resp, provided, encouraged=missing["encouraged"])
    if not form.get("identity_ack"):
        # Shown once: the participant edits the comment or clicks Next again to send it as it is.
        notes = identity.check(resp, engine.project(resp["survey_no"]), visible, provided)
        if notes:
            return _page(request, resp, provided, identity_notes=notes, reminded=bool(form.get("reminded")))
    # Rules and alerts run after the response is sent, so the participant never waits on them.
    engine.advance(response_id, provided, defer=background.add_task)
    return redirect(f"/take/r/{response_id}")


# ── Individual Responses ────────────────────────────────────────────────────

@router.get("/responses/{survey_no}", response_class=HTMLResponse)
def responses(request: Request, survey_no: int) -> HTMLResponse:
    proj = project_or_404(survey_no)
    found = db.rows("SELECT * FROM response WHERE survey_no=? ORDER BY id DESC LIMIT 300", survey_no)
    for resp in found:
        answers = engine.answers(resp["id"])
        resp["score"], resp["available"] = engine.auto_score(survey_no, answers)
        resp["notes"] = db.loads(resp["notes"], [])
        resp["alerts"] = db.val("SELECT COUNT(*) FROM sink_log WHERE response_id=?", resp["id"])
        resp["comment"] = next((a["text"] for a in answers.values() if a.get("text")), "")
    open_questions = [q for q in engine.questions(survey_no) if q["type"] == "text"]
    categories = [{"q": q, **counts} for q in open_questions
                  if (counts := free_text.counts(survey_no, q)) is not None] \
        if modules.is_on(free_text.MODULE) else []
    return render(request, "responses.html", project=proj, tab="responses", responses=found,
                  open_questions=open_questions, categories=categories)


@router.get("/responses/{survey_no}/r/{response_id}", response_class=HTMLResponse)
def response_detail(request: Request, survey_no: int, response_id: int) -> HTMLResponse:
    proj = project_or_404(survey_no)
    resp = _response_or_404(response_id)
    answers = engine.answers(response_id)
    questions = engine.questions(survey_no)
    shown = set(db.loads(resp["shown"], []))
    score, available = engine.auto_score(survey_no, answers)
    sinks = db.rows("SELECT s.*, a.name AS rule FROM sink_log s LEFT JOIN alert_rule a ON a.id=s.rule_id"
                    " WHERE s.response_id=? ORDER BY s.id", response_id)
    return render(
        request, "response.html", project=proj, tab="responses", response=resp,
        rows=[{"q": q, "text": engine.answer_text(q, answers.get(q["qid"])), "shown": q["qid"] in shown}
              for q in questions],
        score=score, available=available, post=engine.post_populated(response_id),
        notes=db.loads(resp["notes"], []), sinks=sinks, classifications=shared_classes.results_for(response_id),
        pii=pii.flags(response_id) if modules.is_on(pii.ANSWER_MODULE) else {},
        inferred=inferred.results_for(response_id),
        virtual=virtual_questions.answers_for(response_id, survey_no)
        if modules.is_on(virtual_questions.MODULE) else [],
        calls=db.val("SELECT COUNT(*) FROM gateway_call_log WHERE response_id=?", response_id))


@router.post("/responses/{survey_no}/r/{response_id}/post-populate")
def save_post_populate(survey_no: int, response_id: int, form: FormData = Depends(form_data)):
    engine.save_post_populate(response_id, form.get("qid"), to_float(form.get("score")),
                              (form.get("feedback") or "").strip())
    return redirect(f"/responses/{survey_no}/r/{response_id}", "Score saved.")


# ── grading list (scope 8 review) ───────────────────────────────────────────

@router.get("/responses/{survey_no}/grading/{qid}", response_class=HTMLResponse)
def grading(request: Request, survey_no: int, qid: str) -> HTMLResponse:
    proj = project_or_404(survey_no)
    question = engine.question_by_qid(survey_no, qid)
    if not question:
        return redirect(f"/responses/{survey_no}", "That question no longer exists.")
    events.emit("grading.opened", survey_no=survey_no, question=question)
    return render(request, "grading.html", project=proj, tab="responses", question=question,
                  rows=quiz_scoring.grading_rows(survey_no, qid), grades=quiz_scoring.GRADE_LABELS,
                  key_points=quiz_scoring.key_points(survey_no, qid),
                  scoring_on=modules.is_on(quiz_scoring.MODULE))


@router.post("/responses/{survey_no}/grading/{qid}")
def grade(survey_no: int, qid: str, form: FormData = Depends(form_data)):
    response_id = int(form.get("response_id"))
    existing = engine.post_populated(response_id).get(qid)
    score = to_float(form.get("score"))
    # A grade counts as final only once the grader confirms it. Changing the
    # suggested score makes it the grader's own.
    unchanged = bool(existing and existing["source"] == "suggestion" and existing["score"] == score)
    engine.save_post_populate(response_id, qid, score, (form.get("feedback") or "").strip(),
                              status="confirmed", source="suggestion" if unchanged else "grader")
    return redirect(f"/responses/{survey_no}/grading/{qid}", f"Grade for response #{response_id} confirmed.")


# ── local sinks ─────────────────────────────────────────────────────────────

@router.get("/sinks", response_class=HTMLResponse)
def sinks(request: Request, sink: str = "email", survey_no: Optional[int] = None) -> HTMLResponse:
    where, args = "s.sink=?", [sink]
    if survey_no:
        where, args = where + " AND s.survey_no=?", args + [survey_no]
    entries = db.rows(f"SELECT s.*, a.name AS rule, p.name AS project, p.anonymous FROM sink_log s"
                      f" LEFT JOIN alert_rule a ON a.id=s.rule_id LEFT JOIN project p ON p.survey_no=s.survey_no"
                      f" WHERE {where} ORDER BY s.id DESC LIMIT 300", *args)
    for entry in entries:
        entry["payload"] = db.loads(entry["payload"], {})
    counts = {r["sink"]: r["n"] for r in db.rows("SELECT sink, COUNT(*) AS n FROM sink_log GROUP BY sink")}
    today = db.val("SELECT COUNT(*) FROM sink_log WHERE sink='email' AND substr(created_at,1,10)=?", db.now()[:10])
    return render(request, "sinks.html", sink=sink, entries=entries, counts=counts, today=today,
                  survey_no=survey_no, sinks=engine.SINKS)
