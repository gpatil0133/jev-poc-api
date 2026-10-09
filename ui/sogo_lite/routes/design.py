"""Projects, Create with AI, and the Design module: questions, Logic, Tags,
Assign Scores, Rules & Alerts."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import FormData

from sogo_lite import create_ai, db, engine, events, modules, seed, suggestions
from sogo_lite.modules import (
    alert_meaning, free_text, logic_text, pii, quiz_scoring, shared_classes, tag_suggest,
    virtual_questions,
)
from sogo_lite.web import back, form_data, project_or_404, redirect, render, to_float, unsynced_class_sets

router = APIRouter()


# ── projects ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return render(request, "projects.html", projects=engine.projects())


@router.post("/projects")
def create_project(form: FormData = Depends(form_data)):
    name = (form.get("name") or "").strip() or "Untitled project"
    survey_no = engine.create_project(name, form.get("type") or "Survey", bool(form.get("anonymous")))
    return redirect(f"/design/{survey_no}", "Project created.")


@router.post("/projects/{survey_no}/delete")
def delete_project(survey_no: int):
    engine.delete_project(survey_no)
    return redirect("/", f"Project {survey_no} deleted.")


@router.get("/create-ai", response_class=HTMLResponse)
def create_ai_form(request: Request) -> HTMLResponse:
    return render(request, "create_ai.html", result=None, prompt="")


@router.post("/create-ai", response_class=HTMLResponse)
def create_ai_generate(request: Request, form: FormData = Depends(form_data)) -> HTMLResponse:
    prompt = (form.get("prompt") or "").strip()
    if not prompt:
        return render(request, "create_ai.html", result=None, prompt="", msg="Type a prompt first.")
    survey_no, decision = create_ai.generate(prompt)
    return render(request, "create_ai.html", prompt=prompt,
                  result={"survey_no": survey_no, "decision": decision, "questions": engine.questions(survey_no)})


# ── Design: questions (5.2) ─────────────────────────────────────────────────

def _question_or_404(survey_no: int, question_id: int) -> dict[str, Any]:
    question = engine.question(question_id)
    if not question or question["survey_no"] != survey_no:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No question {question_id} in project {survey_no}.", "status": 404})
    return question


def _raise_question_saved(question_id: int) -> None:
    question = engine.question(question_id)
    if question:
        events.emit("question.saved", question=question)


@router.get("/design/{survey_no}", response_class=HTMLResponse)
def design(request: Request, survey_no: int) -> HTMLResponse:
    proj = project_or_404(survey_no)
    questions = engine.questions(survey_no)
    hints = {q["id"]: len(suggestions.question_hints(q["id"])) for q in questions}
    class_sets = {cs["qid"]: cs for cs in db.rows("SELECT qid, dirty FROM class_set WHERE survey_no=?", survey_no)}
    return render(request, "design.html", project=proj, tab="questions", pages=engine.pages(survey_no),
                  questions=questions, hints=hints, class_sets=class_sets,
                  unsynced=unsynced_class_sets(survey_no))


@router.post("/design/{survey_no}/pages")
def add_page(survey_no: int):
    project_or_404(survey_no)
    engine.add_page(survey_no)
    return redirect(f"/design/{survey_no}", "Page added.")


@router.post("/design/{survey_no}/questions")
def add_question(survey_no: int, background: BackgroundTasks, form: FormData = Depends(form_data)):
    project_or_404(survey_no)
    wording = (form.get("wording") or "").strip()
    if not wording:
        return redirect(f"/design/{survey_no}", "Give the question some wording.")
    question_id = engine.add_question(
        survey_no, int(form.get("page_id")), form.get("type") or "text", wording,
        options=engine.parse_option_lines(form.get("options") or ""),
        metric_kind=form.get("metric_kind"), required=form.get("required") or "none")
    # After the save completes, so saving the question is never delayed by a hint.
    background.add_task(_raise_question_saved, question_id)
    return redirect(f"/design/{survey_no}/q/{question_id}", "Question saved.")


def _editor(request: Request, survey_no: int, question_id: int, **extra: Any) -> HTMLResponse:
    proj = project_or_404(survey_no)
    question = _question_or_404(survey_no, question_id)
    classes_on = modules.is_on(shared_classes.MODULE) and question["type"] == "text"
    cs = shared_classes.get(survey_no, question["qid"]) if classes_on else None
    draft = extra.pop("draft", None) or {
        "tasks": (cs or {}).get("tasks", []), "custom": (cs or {}).get("custom", []),
        "act": (cs or {}).get("act"), "suggest": (cs or {}).get("suggest"), "model": (cs or {}).get("model")}
    custom_rows = [{"id": s["id"], "type": s["type"], "instructions": s.get("instructions", ""),
                    "lines": "\n".join(f"{k}: {v}" if v else k for k, v in (s.get("classes") or {}).items())}
                   for s in draft["custom"]]
    custom_rows += [{"id": "", "type": "choice", "instructions": "", "lines": ""}] * 2
    return render(
        request, "question.html", project=proj, tab="questions", question=question,
        metric_questions=[q for q in engine.questions(survey_no) if q["type"] == "metric"],
        classes_on=classes_on, class_set=cs, draft=draft, custom_rows=custom_rows,
        catalog=shared_classes.comment_tasks() if classes_on else [],
        options_text="\n".join(f"{o['text']} | {o['points']:g}" if o["points"] else o["text"]
                               for o in question["options"]),
        hints=suggestions.question_hints(question_id),
        categories=free_text.get(survey_no, question["qid"]) if question["type"] == "text" else None, **extra)


@router.get("/design/{survey_no}/q/{question_id}", response_class=HTMLResponse)
def edit_question(request: Request, survey_no: int, question_id: int) -> HTMLResponse:
    return _editor(request, survey_no, question_id)


@router.post("/design/{survey_no}/q/{question_id}")
def save_question(survey_no: int, question_id: int, background: BackgroundTasks,
                  form: FormData = Depends(form_data)):
    question = _question_or_404(survey_no, question_id)
    wording = (form.get("wording") or "").strip() or question["wording"]
    required = form.get("required") if form.get("required") in engine.REQUIRED_MODES else "none"
    is_followup = 1 if form.get("is_followup") else 0
    # The field is only on the form while the module is on; otherwise the mark is kept.
    pii_kind = (form.get("pii_kind") or None) if "pii_kind" in form else question["pii_kind"]
    db.run("UPDATE question SET wording=?, required=?, pii_kind=?, metric_kind=?, is_followup=?, parent_qid=?,"
           " followup_min=?, followup_max=? WHERE id=?",
           wording, required, pii_kind if pii_kind in pii.KINDS else None, form.get("metric_kind") if question["type"] == "metric" else None,
           is_followup if question["type"] == "text" else 0,
           (form.get("parent_qid") or None) if is_followup else None,
           to_float(form.get("followup_min")) if is_followup else None,
           to_float(form.get("followup_max")) if is_followup else None, question_id)
    if question["type"] in ("radio", "checkbox"):
        engine.set_options(question_id, engine.parse_option_lines(form.get("options") or ""))
    background.add_task(_raise_question_saved, question_id)
    return redirect(f"/design/{survey_no}/q/{question_id}", "Question saved.")


@router.post("/design/{survey_no}/q/{question_id}/delete")
def delete_question(survey_no: int, question_id: int):
    _question_or_404(survey_no, question_id)
    engine.delete_question(question_id)
    return redirect(f"/design/{survey_no}", "Question deleted.")


@router.get("/design/{survey_no}/q/{question_id}/hints", response_class=HTMLResponse)
def question_hints(request: Request, survey_no: int, question_id: int) -> HTMLResponse:
    """The hints under a question. The editor polls this, because hints arrive after the save."""
    return render(request, "_hints.html", hints=suggestions.question_hints(question_id))


@router.post("/suggestions/{suggestion_id}/{action}")
def act_on_suggestion(request: Request, suggestion_id: int, action: str):
    if action == "accept":
        applied = suggestions.accept(suggestion_id)
        return back(request, "/", "Suggestion applied." if applied else "That suggestion is no longer pending.")
    suggestions.dismiss(suggestion_id)
    return back(request, "/", "Suggestion dismissed.")


# ── scope 1: the Classes tab ────────────────────────────────────────────────

@router.post("/design/{survey_no}/q/{question_id}/classes", response_class=HTMLResponse)
def save_classes(request: Request, survey_no: int, question_id: int, form: FormData = Depends(form_data)):
    question = _question_or_404(survey_no, question_id)
    here = f"/design/{survey_no}/q/{question_id}#classes"
    action = form.get("action")
    if action == "verify":
        return redirect(here, shared_classes.verify(survey_no, question["qid"])[1])
    if action == "refresh":
        return redirect(here, f"Catalog refreshed: {len(shared_classes.catalog(refresh=True))} tasks.")
    custom, errors = shared_classes.parse_custom_form(form)
    draft = {"tasks": form.getlist("tasks"), "custom": custom, "act": to_float(form.get("act")),
             "suggest": to_float(form.get("suggest")), "model": form.get("model") or None}
    if errors:
        return _editor(request, survey_no, question_id, draft=draft, msg=" ".join(errors))
    if action == "check":
        result = shared_classes.check_budget(survey_no, draft["tasks"], custom, draft["model"])
        return _editor(request, survey_no, question_id, draft=draft,
                       budget=result.data.get("budget") if not result.fallback else None,
                       budget_error=("; ".join(result.detail.get("errors") or []) or result.message)
                       if result.fallback else None)
    _, message = shared_classes.save(survey_no, question["qid"], draft["tasks"], custom,
                                     draft["act"], draft["suggest"], draft["model"])
    return redirect(here, message)


# ── Design: Logic (5.3) ─────────────────────────────────────────────────────

def _describe_logic(rule: dict[str, Any], by_qid: dict[str, dict[str, Any]]) -> str:
    source = by_qid.get(rule["source_qid"])
    if rule["op"] in engine.OPTION_OPS:
        option = next((o["text"] for o in (source or {}).get("options", []) if str(o["id"]) == rule["operand"]), "?")
        condition = f"\"{option}\" {'is picked' if rule['op'] == 'picked' else 'is not picked'}"
    elif rule["op"] == logic_text.OP:
        condition = f"answer is about {rule['operand'].replace('|', ' = ')}" + (
            f" (min {rule['min_prob']:.2f})" if rule["min_prob"] is not None else " (band act)")
    elif rule["op"] == virtual_questions.SKIP_OP:
        vq = virtual_questions.get(int(rule["operand"])) if str(rule["operand"]).isdigit() else None
        condition = f"comment already answers \"{vq['wording'] if vq else '?'}\" (inferred, band act)"
    else:
        condition = engine.TEXT_OPS.get(rule["op"], rule["op"]) + (
            f" \"{rule['operand']}\"" if rule["op"] not in ("answered", "not_answered") else "")
    if rule["action"] in ("show", virtual_questions.SKIP_ACTION):
        target = f"{rule['action']} {rule['target']}: {(by_qid.get(rule['target']) or {}).get('wording', '?')}"
    else:
        target = f"jump to page {rule['target']}"
    return f"If {rule['source_qid']} {condition} → {target}"


@router.get("/design/{survey_no}/logic", response_class=HTMLResponse)
def logic(request: Request, survey_no: int, src: Optional[str] = None) -> HTMLResponse:
    proj = project_or_404(survey_no)
    questions = engine.questions(survey_no)
    by_qid = {q["qid"]: q for q in questions}
    source = by_qid.get(src or "")
    about_on = modules.is_on(logic_text.MODULE) and bool(source) and source["type"] == "text"
    rules = [{"id": r["id"], "op": r["op"], "text": _describe_logic(r, by_qid),
              "inactive": (r["op"] == logic_text.OP and not modules.is_on(logic_text.MODULE))
              or (r["op"] == virtual_questions.SKIP_OP and not modules.is_on(virtual_questions.MODULE))}
             for r in engine.logic_rules(survey_no)]
    return render(
        request, "logic.html", project=proj, tab="logic", questions=questions, rules=rules, source=source,
        pages=engine.pages(survey_no), option_ops=engine.OPTION_OPS, text_ops=engine.TEXT_OPS,
        about_on=about_on, about_label=logic_text.OP_LABEL,
        ready_made=alert_meaning.ready_made() if about_on else [],
        class_refs=shared_classes.class_refs(shared_classes.get(survey_no, source["qid"])) if about_on else [])


@router.post("/design/{survey_no}/logic")
def add_logic(survey_no: int, form: FormData = Depends(form_data)):
    project_or_404(survey_no)
    source_qid, op = form.get("source_qid") or "", form.get("op") or ""
    here = f"/design/{survey_no}/logic?src={source_qid}"
    if op == logic_text.OP:
        if not form.get("target_q"):
            return redirect(here, "Pick the question to show.")
        _, message = logic_text.add_rule(
            survey_no, source_qid, form.get("about_source") or "topic",
            {"class": form.get("class_ref"), "ready": form.get("task_id")}.get(form.get("about_source"),
                                                                              form.get("topic")),
            form.get("description") or "", form.get("target_q"), to_float(form.get("min_prob")))
        return redirect(here, message)
    action = form.get("action") if form.get("action") in engine.ACTIONS else "show"
    target = form.get("target_q") if action == "show" else form.get("target_page")
    if not target:
        return redirect(here, "Pick a target for the rule.")
    operand = form.get("option_id") if op in engine.OPTION_OPS else (form.get("operand") or "").strip()
    db.run("INSERT INTO logic_rule(survey_no, source_qid, op, operand, action, target) VALUES(?,?,?,?,?,?)",
           survey_no, source_qid, op, operand, action, target)
    return redirect(here, "Rule added.")


@router.post("/design/{survey_no}/logic/{rule_id}/delete")
def delete_logic(survey_no: int, rule_id: int):
    db.run("DELETE FROM logic_rule WHERE id=? AND survey_no=?", rule_id, survey_no)
    return redirect(f"/design/{survey_no}/logic", "Rule deleted.")


# ── Design: Tags (5.4) and scope 6 ──────────────────────────────────────────

@router.get("/design/{survey_no}/tags", response_class=HTMLResponse)
def tags(request: Request, survey_no: int) -> HTMLResponse:
    proj = project_or_404(survey_no)
    return render(
        request, "tags.html", project=proj, tab="tags", categories=engine.tag_categories(),
        questions=engine.questions(survey_no), option_tags=engine.option_tags(survey_no),
        question_tags=engine.question_tags(survey_no), review=tag_suggest.review_list(survey_no)
        if modules.is_on(tag_suggest.MODULE) else [],
        max_categories=engine.MAX_TAG_CATEGORIES, max_tags=engine.MAX_TAGS_PER_CATEGORY)


@router.post("/design/{survey_no}/tags/categories")
def add_category(survey_no: int, form: FormData = Depends(form_data)):
    error = engine.add_tag_category(form.get("name") or "")
    return redirect(f"/design/{survey_no}/tags", error or "Category added.")


@router.post("/design/{survey_no}/tags/categories/{category_id}")
def add_tags(survey_no: int, category_id: int, form: FormData = Depends(form_data)):
    added, refused = engine.add_tags(category_id, (form.get("names") or "").replace(",", "\n").splitlines())
    message = f"{added} tag(s) added." + (
        f" {refused} refused: a category holds at most {engine.MAX_TAGS_PER_CATEGORY} tags." if refused else "")
    return redirect(f"/design/{survey_no}/tags", message)


@router.post("/design/{survey_no}/tags/assign")
def assign_tag(survey_no: int, form: FormData = Depends(form_data)):
    target_type, _, target_id = (form.get("target") or "").partition(":")
    tag_id = form.get("tag_id")
    if not target_id or not tag_id:
        return redirect(f"/design/{survey_no}/tags", "Pick an item and a tag.")
    if target_type == "option":
        engine.tag_option(int(target_id), int(tag_id))
    else:
        engine.tag_question(int(target_id), int(tag_id))
    return redirect(f"/design/{survey_no}/tags", "Tag applied.")


@router.post("/design/{survey_no}/tags/unassign")
def unassign_tag(survey_no: int, form: FormData = Depends(form_data)):
    table, column = ("option_tag", "option_id") if form.get("target_type") == "option" \
        else ("question_tag", "question_id")
    db.run(f"DELETE FROM {table} WHERE {column}=? AND tag_id=?", int(form.get("target_id")), int(form.get("tag_id")))
    return redirect(f"/design/{survey_no}/tags", "Tag removed.")


@router.post("/design/{survey_no}/tags/automap")
def automap(survey_no: int):
    return redirect(f"/design/{survey_no}/tags", f"Auto-map tagged {engine.automap(survey_no)} answer option(s).")


@router.post("/design/{survey_no}/tags/suggest")
def suggest_tags(survey_no: int):
    project_or_404(survey_no)
    if not modules.is_on(tag_suggest.MODULE):
        return redirect(f"/design/{survey_no}/tags", "The tag suggestions module is off.")
    s = tag_suggest.suggest(survey_no)
    if s["unavailable"] and not s["created"]:
        message = "No suggestions available. Manual tagging and exact auto-map are unaffected."
    else:
        message = (f"{s['created']} suggestion(s). Items sent with the full tag list: {s['full']}; with a "
                   f"shortlist: {s['shortlist']}; with no candidate tag: {s['no_candidates']}.")
    if s["automapped"]:
        message = f"Auto-map tagged {s['automapped']} option(s) first. " + message
    return redirect(f"/design/{survey_no}/tags#review", message)


@router.post("/design/{survey_no}/tags/review")
def review_tags(survey_no: int, form: FormData = Depends(form_data)):
    if form.get("action") == "dismiss":
        db.run("UPDATE suggestion SET status='dismissed' WHERE kind='tag' AND survey_no=? AND status='pending'",
               survey_no)
        return redirect(f"/design/{survey_no}/tags", "Suggestions dismissed.")
    applied = sum(1 for sid in form.getlist("accept") if suggestions.accept(int(sid)))
    return redirect(f"/design/{survey_no}/tags", f"{applied} suggested tag(s) applied.")


# ── Design: Assign Scores (5.5) and scope 8 set-up ──────────────────────────

@router.get("/design/{survey_no}/scores", response_class=HTMLResponse)
def scores(request: Request, survey_no: int) -> HTMLResponse:
    proj = project_or_404(survey_no)
    questions = engine.questions(survey_no)
    return render(request, "scores.html", project=proj, tab="scores", questions=questions,
                  key_points={q["qid"]: quiz_scoring.key_points(survey_no, q["qid"])
                              for q in questions if q["type"] == "text"})


@router.post("/design/{survey_no}/scores")
def save_scores(survey_no: int, form: FormData = Depends(form_data)):
    project_or_404(survey_no)
    for q in engine.questions(survey_no):
        for option in q["options"]:
            points = to_float(form.get(f"pts_{option['id']}"))
            if points is not None:
                db.run("UPDATE answer_option SET points=? WHERE id=?", points, option["id"])
        if q["type"] == "text":
            db.run("UPDATE question SET max_points=? WHERE id=?", to_float(form.get(f"max_{q['id']}")) or 0, q["id"])
            if f"kps_{q['id']}" in form:
                quiz_scoring.save_key_points(survey_no, q["qid"], form.get(f"model_{q['id']}") or "",
                                             (form.get(f"kps_{q['id']}") or "").splitlines())
    return redirect(f"/design/{survey_no}/scores", "Scores saved.")


# ── Design: Rules & Alerts (5.6) and scope 2 ────────────────────────────────

@router.get("/design/{survey_no}/alerts", response_class=HTMLResponse)
def alerts(request: Request, survey_no: int) -> HTMLResponse:
    proj = project_or_404(survey_no)
    return render(request, "alerts.html", project=proj, tab="alerts", rules=engine.alert_rules(survey_no),
                  rule=None, unsynced=unsynced_class_sets(survey_no))


@router.post("/design/{survey_no}/alerts")
def create_alert(survey_no: int, form: FormData = Depends(form_data)):
    project_or_404(survey_no)
    rule_id = db.run("INSERT INTO alert_rule(survey_no, name) VALUES(?,?)", survey_no,
                     (form.get("name") or "").strip() or "New rule")
    return redirect(f"/design/{survey_no}/alerts/{rule_id}")


@router.get("/design/{survey_no}/alerts/{rule_id}", response_class=HTMLResponse)
def alert_editor(request: Request, survey_no: int, rule_id: int, cq: Optional[str] = None) -> HTMLResponse:
    proj = project_or_404(survey_no)
    rule = engine.alert_rule(rule_id)
    if not rule or rule["survey_no"] != survey_no:
        return redirect(f"/design/{survey_no}/alerts", "That rule no longer exists.")
    questions = engine.questions(survey_no)
    meaning_on = modules.is_on(alert_meaning.MODULE)
    comment = next((q for q in questions if q["qid"] == cq and q["type"] == "text"), None)
    return render(
        request, "alerts.html", project=proj, tab="alerts", rules=engine.alert_rules(survey_no), rule=rule,
        questions=questions, sinks=engine.SINKS, meaning_on=meaning_on, comment=comment,
        class_refs=shared_classes.class_refs(shared_classes.get(survey_no, comment["qid"])) if comment else [],
        ready_made=alert_meaning.ready_made() if meaning_on and comment else [],
        default_min_prob=alert_meaning.DEFAULT_MIN_PROB, unsynced=unsynced_class_sets(survey_no))


def _new_condition(survey_no: int, form: FormData) -> tuple[Optional[dict[str, Any]], str]:
    ctype = form.get("ctype")
    by_qid = {q["qid"]: q for q in engine.questions(survey_no)}
    if ctype == "picked":
        option = db.one("SELECT o.id, o.text, q.qid FROM answer_option o JOIN question q ON q.id=o.question_id"
                        " WHERE o.id=? AND q.survey_no=?", form.get("option_id") or 0, survey_no)
        if not option:
            return None, "Pick an answer option."
        return {"type": "picked", "qid": option["qid"], "option_id": option["id"],
                "label": f"{option['qid']} answer is \"{option['text']}\""}, ""
    if ctype in ("count", "score"):
        q, value = by_qid.get(form.get("qid") or ""), to_float(form.get("value"))
        op = form.get("op") if form.get("op") in (">=", "<=", "=") else ">="
        if not q or value is None:
            return None, "Pick a question and give a number."
        what = "answers picked" if ctype == "count" else engine.METRICS.get(q["metric_kind"] or "", ("score",))[0]
        return {"type": ctype, "qid": q["qid"], "op": op, "value": value,
                "label": f"{q['qid']} {what} {op} {value:g}"}, ""
    if ctype == alert_meaning.CONDITION:
        return alert_meaning.build_condition(
            survey_no, form.get("qid") or "", form.get("source") or "own",
            {"class": form.get("class_ref"), "ready": form.get("task_id")}.get(form.get("source"), form.get("own")),
            to_float(form.get("min_prob")))
    return None, "Unknown condition type."


@router.post("/design/{survey_no}/alerts/{rule_id}")
def update_alert(survey_no: int, rule_id: int, form: FormData = Depends(form_data)):
    rule = engine.alert_rule(rule_id)
    if not rule or rule["survey_no"] != survey_no:
        return redirect(f"/design/{survey_no}/alerts", "That rule no longer exists.")
    here, message, action = f"/design/{survey_no}/alerts/{rule_id}", "Rule saved.", form.get("action")
    if action == "delete":
        db.run("DELETE FROM alert_rule WHERE id=?", rule_id)
        return redirect(f"/design/{survey_no}/alerts", "Rule deleted.")
    if action == "rename":
        rule["name"] = (form.get("name") or "").strip() or rule["name"]
        rule["enabled"] = 1 if form.get("enabled") else 0
    elif action == "add_condition":
        condition, error = _new_condition(survey_no, form)
        if condition is None:
            return redirect(here, error)
        rule["conditions"].append(condition)
        message = "Condition added."
    elif action == "del_condition":
        del rule["conditions"][int(form.get("index")):int(form.get("index")) + 1]
    elif action == "add_action":
        if form.get("sink") in engine.SINKS:
            rule["actions"].append({"type": form.get("sink"), "target": (form.get("target") or "").strip()})
    elif action == "del_action":
        del rule["actions"][int(form.get("index")):int(form.get("index")) + 1]
    engine.save_alert_rule(rule)
    return redirect(here, message)


@router.post("/design/{survey_no}/sync-classes")
def sync_classes(survey_no: int, request: Request):
    counts = seed.sync_class_sets()
    return back(request, f"/design/{survey_no}",
                f"Class sets saved to the gateway: {counts['saved']}; failed: {counts['failed']}.")
