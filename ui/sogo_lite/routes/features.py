"""The feature concepts (docs/JEV-Feature-concepts.html): category lists, Virtual
Questions, Survey Coach after launch, Feedback Owners, Fix Tracker and the
Distribute stand-in for invitation hold and replies."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import FormData

from sogo_lite import db, engine, modules
from sogo_lite.modules import (
    distribution, fix_tracker, free_text, inferred, owners, survey_coach, virtual_questions,
)
from sogo_lite.web import form_data, project_or_404, redirect, render, to_float

router = APIRouter()


def _survey_nos(form: FormData, field: str = "projects") -> list[int]:
    return [int(v) for v in form.getlist(field) if str(v).isdigit()]


def _backfill(feature: str) -> str:
    totals = {"answers": 0, "failed": 0}
    for survey_no in sorted(owners.enabled_projects(feature)):
        counts = inferred.backfill(survey_no)
        totals = {k: totals[k] + counts[k] for k in totals}
    return (f"{totals['answers']} text answer(s) read."
            + (f" {totals['failed']} could not be read: the gateway did not answer." if totals["failed"] else ""))


# ── free text to category ───────────────────────────────────────────────────

@router.post("/design/{survey_no}/q/{question_id}/categories")
def save_categories(survey_no: int, question_id: int, form: FormData = Depends(form_data)):
    project_or_404(survey_no)
    question = engine.question(question_id)
    here = f"/design/{survey_no}/q/{question_id}#categories"
    if not question or question["survey_no"] != survey_no or question["type"] != "text":
        return redirect(f"/design/{survey_no}", "A category list goes on a Text Box.")
    _, message = free_text.save(survey_no, question["qid"], form.get("instructions") or "", form.get("lines") or "")
    return redirect(here, message)


# ── Virtual Questions ───────────────────────────────────────────────────────

def _virtual_page(request: Request, survey_no: int, **extra: Any) -> HTMLResponse:
    proj = project_or_404(survey_no)
    questions = engine.questions(survey_no)
    found = virtual_questions.for_project(survey_no)
    for vq in found:
        vq["summary"] = virtual_questions.summary(vq)
        vq["skips"] = [r for r in engine.logic_rules(survey_no)
                       if r["op"] == virtual_questions.SKIP_OP and r["operand"] == str(vq["id"])]
    return render(request, "virtual.html", project=proj, tab="virtual", vqs=found, questions=questions,
                  comment_questions=survey_coach.comment_questions(survey_no),
                  alert_rules=engine.alert_rules(survey_no), on=modules.is_on(virtual_questions.MODULE),
                  sample_size=virtual_questions.SAMPLE_SIZE, default_min_prob=virtual_questions.DEFAULT_MIN_PROB,
                  not_mentioned=virtual_questions.NOT_MENTIONED, **extra)


@router.get("/design/{survey_no}/virtual", response_class=HTMLResponse)
def virtual(request: Request, survey_no: int) -> HTMLResponse:
    return _virtual_page(request, survey_no)


@router.post("/design/{survey_no}/virtual")
def add_virtual(survey_no: int, form: FormData = Depends(form_data)):
    project_or_404(survey_no)
    _, message = virtual_questions.add(survey_no, form.get("source_qid") or "", form.get("wording") or "",
                                       form.get("lines") or "", going_forward=bool(form.get("going_forward")))
    return redirect(f"/design/{survey_no}/virtual", message)


@router.post("/design/{survey_no}/virtual/{vq_id}", response_class=HTMLResponse)
def act_on_virtual(request: Request, survey_no: int, vq_id: int, form: FormData = Depends(form_data)):
    here = f"/design/{survey_no}/virtual"
    vq = virtual_questions.get(vq_id)
    if not vq or vq["survey_no"] != survey_no:
        return redirect(here, "That Virtual Question no longer exists.")
    action = form.get("action")
    if action == "delete":
        virtual_questions.delete(vq_id)
        return redirect(here, "Virtual Question deleted. Its stored answers are no longer read.")
    if action == "preview":
        rows, message = virtual_questions.preview(vq)
        return _virtual_page(request, survey_no, preview={"vq_id": vq_id, "rows": rows}, msg=message or None)
    if action in ("sample", "all"):
        done = virtual_questions.run(vq, virtual_questions.SAMPLE_SIZE if action == "sample" else None)
        return redirect(here, f"{done['stored']} inferred answer(s) stored of {done['sent']} comment(s) sent."
                        + (f" {done['failed']} not answered: the gateway did not answer." if done["failed"] else "")
                        + (f" {done['unsure']} left unanswered: unsure." if done["unsure"] else ""))
    if action == "forward":
        db.run("UPDATE virtual_question SET going_forward=? WHERE id=?", 1 if form.get("going_forward") else 0, vq_id)
        return redirect(here, "Saved.")
    if action == "alert":
        rule = engine.alert_rule(int(form.get("rule_id") or 0))
        condition, error = virtual_questions.build_condition(vq, form.get("answer") or "",
                                                             to_float(form.get("min_prob")))
        if not rule or rule["survey_no"] != survey_no or condition is None:
            return redirect(here, error or "Pick a rule. Create one under Rules & Alerts first.")
        rule["conditions"].append(condition)
        engine.save_alert_rule(rule)
        return redirect(here, f"Condition added to the rule \"{rule['name']}\".")
    if action == "skip":
        _, message = virtual_questions.add_skip(vq, form.get("target_qid") or "")
        return redirect(here, message)
    return redirect(here)


# ── Survey Coach, after launch ──────────────────────────────────────────────

@router.get("/design/{survey_no}/coach", response_class=HTMLResponse)
def coach(request: Request, survey_no: int) -> HTMLResponse:
    proj = project_or_404(survey_no)
    run = survey_coach.latest_blind_spot(survey_no)
    by_qid = {q["qid"]: q for q in engine.questions(survey_no)}
    return render(request, "coach.html", project=proj, tab="coach", run=run, by_qid=by_qid,
                  comment_questions=survey_coach.comment_questions(survey_no),
                  closed=survey_coach.closed_questions(survey_no), on=modules.is_on(survey_coach.BLIND_SPOT_MODULE),
                  sample_size=survey_coach.SAMPLE_SIZE, min_share=survey_coach.MIN_NONE_SHARE)


@router.post("/design/{survey_no}/coach")
def run_coach(survey_no: int, form: FormData = Depends(form_data)):
    project_or_404(survey_no)
    if not modules.is_on(survey_coach.BLIND_SPOT_MODULE):
        return redirect(f"/design/{survey_no}/coach", "The Survey Coach blind-spot module is off.")
    _, message = survey_coach.run_blind_spot(survey_no, form.get("qid") or "", form.get("topics") or "")
    return redirect(f"/design/{survey_no}/coach", message)


# ── Feedback Owners ─────────────────────────────────────────────────────────

@router.get("/owners", response_class=HTMLResponse)
def owners_page(request: Request, team: Optional[str] = None) -> HTMLResponse:
    box = owners.inbox()
    return render(request, "owners.html", teams=owners.teams(), inbox=box, team=team if team in box["teams"] else None,
                  projects=engine.projects(), enabled=owners.enabled_projects(), on=modules.is_on(owners.MODULE))


@router.post("/owners")
def owners_action(form: FormData = Depends(form_data)):
    action = form.get("action")
    if action == "teams":
        return redirect("/owners", owners.save_teams(form.get("lines") or "")[1])
    if action == "projects":
        owners.set_projects(_survey_nos(form))
        return redirect("/owners", "Projects saved. New responses are routed when they are submitted.")
    if action == "backfill":
        return redirect("/owners", _backfill(owners.FEATURE))
    if action == "digest":
        return redirect("/owners", f"{owners.send_digests()} digest email(s) written to the outbox.")
    return redirect("/owners")


# ── Fix Tracker ─────────────────────────────────────────────────────────────

@router.get("/fix-tracker", response_class=HTMLResponse)
def fix_tracker_page(request: Request) -> HTMLResponse:
    return render(request, "fix_tracker.html", issues=fix_tracker.issues(), report=fix_tracker.report(),
                  cases=fix_tracker.cases(), projects=engine.projects(), statuses=fix_tracker.STATUSES,
                  enabled=owners.enabled_projects(fix_tracker.FEATURE), on=modules.is_on(fix_tracker.MODULE))


@router.post("/fix-tracker")
def fix_tracker_action(form: FormData = Depends(form_data)):
    action = form.get("action")
    if action == "issues":
        return redirect("/fix-tracker", fix_tracker.save_issues(form.get("lines") or "")[1])
    if action == "projects":
        owners.set_projects(_survey_nos(form), fix_tracker.FEATURE)
        return redirect("/fix-tracker", "Projects saved. New comments are checked when they are submitted.")
    if action == "backfill":
        return redirect("/fix-tracker", _backfill(fix_tracker.FEATURE))
    return redirect("/fix-tracker")


# ── Distribute: invitation hold and replies ─────────────────────────────────

@router.get("/distribute", response_class=HTMLResponse)
def distribute(request: Request) -> HTMLResponse:
    return render(request, "distribute.html", contacts=distribution.contacts(),
                  invitations=distribution.invitations(), replies=distribution.replies(),
                  projects=engine.projects(), statuses=distribution.STATUSES, labels=distribution.REPLY_LABELS,
                  hold_on=modules.is_on(distribution.HOLD_MODULE), reply_on=modules.is_on(distribution.REPLY_MODULE))


@router.post("/distribute")
def distribute_action(form: FormData = Depends(form_data)):
    action = form.get("action")
    contact_id = int(form.get("contact_id")) if str(form.get("contact_id") or "").isdigit() else 0
    if action == "contact":
        added = distribution.add_contact(form.get("name") or "", form.get("email") or "", form.get("note") or "")
        return redirect("/distribute", "Contact added." if added else "Give a name and an email address.")
    if action == "activity":
        distribution.add_activity(contact_id, form.get("note") or "")
        return redirect("/distribute", "Activity added.")
    if action == "queue":
        survey_no = int(form.get("survey_no")) if str(form.get("survey_no") or "").isdigit() else 0
        if not engine.project(survey_no):
            return redirect("/distribute", "Pick a project.")
        counts = distribution.queue(survey_no)
        return redirect("/distribute#invitations", f"Sent {counts['sent']}, held {counts['held']}, "
                                                   f"not sent {counts['not_sent']}.")
    if action == "reply":
        return redirect("/distribute#replies", distribution.receive_reply(contact_id, form.get("text") or ""))
    return redirect("/distribute")
