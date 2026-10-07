"""The test bench itself: Inspector, Settings (module toggles, fault switches),
the jobs polling test and the scenario runner."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.datastructures import FormData

from sogo_lite import db, engine, gateway, modules, scenarios, seed
from sogo_lite.config import get_settings
from sogo_lite.modules import quiz_scoring
from sogo_lite.web import form_data, redirect, render

router = APIRouter()

TEST_QUESTION = {"id": "topic", "type": "choice", "classes": {
    "price": "cost, fees, value for money",
    "delivery": "shipping time, courier, damaged on arrival",
    "staff": "behaviour or helpfulness of employees"}}
TEST_TEXT = "The courier left my parcel in the rain and it arrived a week late."


# ── Inspector (9.1) ─────────────────────────────────────────────────────────

def _answers_summary(call: dict[str, Any]) -> list[dict[str, Any]]:
    data = db.loads(call["response"], {}) or {}
    if data.get("results") is not None:
        return [{"id": f"{len(data['results'])} results", "label": "", "confidence": None, "band": "",
                 "forced": False}]
    return [{"id": answer_id, "label": a.get("label"), "confidence": a.get("confidence"), "band": a.get("band"),
             "forced": bool(a.get("_forced"))} for answer_id, a in (data.get("answers") or {}).items()]


@router.get("/inspector", response_class=HTMLResponse)
def inspector(request: Request, module: str = "", survey_no: str = "", response_id: str = "",
              lo: str = "", hi: str = "") -> HTMLResponse:
    where, args = ["1=1"], []
    if module:
        where.append("('+' || module || '+') LIKE ?")
        args.append(f"%+{module}+%")
    for column, value in (("survey_no", survey_no), ("response_id", response_id)):
        if value.strip().isdigit():
            where.append(f"{column}=?")
            args.append(int(value))
    if lo.isdigit():
        where.append("id>?")
        args.append(int(lo))
    if hi.isdigit():
        where.append("id<=?")
        args.append(int(hi))
    matching = db.rows(f"SELECT * FROM gateway_call_log WHERE {' AND '.join(where)} ORDER BY id DESC", *args)
    calls = matching[:200]
    for call in calls:
        call["answers"] = _answers_summary(call)
    return render(request, "inspector.html", calls=calls, summary=gateway.summarise(matching),
                  total=len(matching), filters={"module": module, "survey_no": survey_no,
                                                "response_id": response_id, "lo": lo, "hi": hi},
                  projects=engine.projects(), timeouts=get_settings().timeouts_ms)


@router.get("/inspector/{call_id}", response_class=HTMLResponse)
def inspector_detail(request: Request, call_id: int, compiled: Optional[str] = None) -> HTMLResponse:
    call = db.one("SELECT * FROM gateway_call_log WHERE id=?", call_id)
    if not call:
        return redirect("/inspector", "That call is no longer in the log.")
    call["answers"] = _answers_summary(call)
    shown = db.one("SELECT * FROM gateway_call_log WHERE id=?", int(compiled)) if compiled and compiled.isdigit() \
        else None
    return render(request, "inspector_detail.html", call=call, compiled=shown,
                  can_compile=call["endpoint"] in ("/v1/classify", "/v1/classify/batch"))


@router.post("/inspector/{call_id}/compiled")
def inspector_compiled(call_id: int):
    """Dry-run the same question set through /v1/tasks/compile to show what went to the model."""
    call = db.one("SELECT * FROM gateway_call_log WHERE id=?", call_id)
    if not call:
        return redirect("/inspector")
    request_body = db.loads(call["request"], {}) or {}
    body: dict[str, Any] = {k: request_body[k] for k in ("survey_no", "question_id", "tasks", "questions", "model")
                            if request_body.get(k) not in (None, [], "")}
    sample = request_body if "text" in request_body else (request_body.get("items") or [{}])[0]
    if sample.get("text"):
        body["sample"] = {k: sample[k] for k in ("text", "survey_question", "metric_score", "context")
                          if sample.get(k) is not None}
    result = gateway.call("POST", "/v1/tasks/compile", body, module="inspector",
                          trigger=f"show compiled request for call #{call_id}", kind="author",
                          survey_no=call["survey_no"])
    gateway.set_outcome(result.log_id, "compiled request shown" if not result.fallback else "compile failed")
    return redirect(f"/inspector/{call_id}?compiled={result.log_id}#compiled")


@router.post("/inspector/clear")
def inspector_clear():
    db.run("DELETE FROM gateway_call_log")
    return redirect("/inspector", "Call log cleared.")


# ── Settings: module toggles and fault switches (9.2) ───────────────────────

@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    return render(request, "settings.html", states=modules.states(), faults=gateway.FAULTS,
                  fault_values=gateway.fault_settings(), health=gateway.health(force=True),
                  timeouts=get_settings().timeouts_ms, unsynced=db.val("SELECT COUNT(*) FROM class_set WHERE dirty=1"),
                  db_file=str(get_settings().db_file))


@router.post("/settings/modules")
def save_modules(form: FormData = Depends(form_data)):
    for module_id in modules.MODULE_IDS:
        modules.set_on(module_id, bool(form.get(module_id)))
    return redirect("/settings", "Module toggles saved.")


@router.post("/settings/faults")
def save_faults(form: FormData = Depends(form_data)):
    scopes = {"", "all", *modules.MODULE_IDS}
    for fault in gateway.FAULTS:
        scope = form.get(fault["id"]) or ""
        db.set_setting(f"fault.{fault['id']}", scope if scope in scopes else "")
    slow_ms = form.get("slow_ms") or "0"
    db.set_setting("fault.slow_ms", slow_ms if slow_ms.isdigit() else "0")
    return redirect("/settings", "Fault switches saved.")


@router.post("/settings/test-call")
def test_call():
    result = gateway.call("POST", "/v1/classify", {"text": TEST_TEXT, "questions": [TEST_QUESTION]},
                          module="settings", trigger="Test call", kind="author")
    gateway.set_outcome(result.log_id, "test call")
    return redirect(f"/inspector/{result.log_id}")


@router.post("/settings/sync")
def sync_all():
    counts = seed.sync_class_sets()
    return redirect("/settings", f"Class sets saved to the gateway: {counts['saved']}; failed: {counts['failed']}.")


@router.post("/settings/reseed")
def reseed():
    seed.reseed()
    counts = seed.sync_class_sets()
    return redirect("/settings", "Seed data restored; projects, responses and the call log were reset. "
                                 f"Class sets saved to the gateway: {counts['saved']}; failed: {counts['failed']}.")


@router.get("/api/health")
def api_health() -> JSONResponse:
    return JSONResponse(gateway.health())


# ── jobs polling test (scope 8) ─────────────────────────────────────────────

@router.get("/jobs", response_class=HTMLResponse)
def jobs(request: Request, job_id: Optional[str] = None) -> HTMLResponse:
    return render(request, "jobs.html", job_id=job_id, default_items=300,
                  quiz=engine.project(seed.QUIZ) is not None)


@router.post("/jobs")
def start_job(form: FormData = Depends(form_data)):
    total = form.get("items") or "300"
    result = quiz_scoring.start_job(seed.QUIZ, "q3", min(5000, max(1, int(total) if total.isdigit() else 300)))
    if result.fallback:
        return redirect("/jobs", f"The job was not accepted ({result.message}).")
    return redirect(f"/jobs?job_id={result.data['job_id']}")


@router.get("/api/jobs/{job_id}")
def api_job(job_id: str) -> JSONResponse:
    result = quiz_scoring.job_status(job_id)
    return JSONResponse({"fallback": result.fallback, "reason": result.reason, **result.data})


# ── scenario runner (10.3) ──────────────────────────────────────────────────

@router.get("/scenarios", response_class=HTMLResponse)
def scenario_page(request: Request) -> HTMLResponse:
    run = scenarios.last_run()
    scopes: dict[str, list[scenarios.Scenario]] = {}
    for scenario in scenarios.SCENARIOS:
        scopes.setdefault(scenario.scope, []).append(scenario)
    return render(request, "scenarios.html", run=run, scopes=scopes, columns=scenarios.COLUMNS,
                  state=scenarios.STATE)


@router.post("/scenarios/run")
def scenario_run():
    started = scenarios.start()
    return redirect("/scenarios", None if started else "A run is already in progress.")


@router.get("/api/scenarios/status")
def scenario_status() -> JSONResponse:
    return JSONResponse(scenarios.STATE)


@router.get("/scenarios/export.csv")
def scenario_csv():
    run = scenarios.last_run()
    if not run:
        return redirect("/scenarios", "Nothing to export yet.")
    return PlainTextResponse(scenarios.export_csv(run), media_type="text/csv", headers={
        "Content-Disposition": f"attachment; filename=sogo-lite-scenarios-run{run['id']}.csv"})


@router.get("/scenarios/export.json")
def scenario_json():
    run = scenarios.last_run()
    if not run:
        return redirect("/scenarios", "Nothing to export yet.")
    return JSONResponse({"run": run["id"], "started_at": run["started_at"], "finished_at": run["finished_at"],
                         "columns": [{"id": c, "name": n} for c, n, _ in scenarios.COLUMNS],
                         "checks": [{"id": s.id, "scope": s.scope, "title": s.title,
                                     **run["results"].get(s.id, {})} for s in scenarios.SCENARIOS],
                         "latency_normal": run["latency"]})
