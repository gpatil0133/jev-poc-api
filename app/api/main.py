"""
app/api/main.py
Laya PoC gateway: a thin FastAPI layer in front of stock laya-serve.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request

from app.backends.base import BackendError
from app.backends.laya import LayaBackend
from app.core.auth import assert_production_safety, get_tenant_id
from app.core.config import get_settings
from app.models.schemas import (
    BatchRequest, BatchResponse, Binding, BindingSaved, ClassifyRequest, ClassifyResponse,
    CompileRequest, CompileResponse, JobStatus, MapColumnsRequest, MapColumnsResponse,
    QuestionSelector,
)
from app.registry.bindings import BindingStore
from app.registry.compiler import CompileError, CompiledSet, compile_set
from app.registry.loader import load_registry
from app.services.classify import ClassifyService
from app.services.decision_log import DecisionLog
from app.services.jobs import JobRunner
from app.services.mapping import MappingService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    assert_production_safety()
    backend = getattr(app.state, "backend", None)      # tests inject a fake
    owns_backend = backend is None
    if owns_backend:
        backend = LayaBackend(settings.laya_base_url, settings.laya_api_key, settings.batch_timeout_s)
    registry = load_registry()
    service = ClassifyService(
        backend, registry, BindingStore(settings.data_dir), settings,
        DecisionLog(settings.log_dir, settings.log_text),
    )
    app.state.backend = backend
    app.state.registry = registry
    app.state.service = service
    app.state.jobs = JobRunner(service, settings.data_dir, settings.batch_max_items)
    app.state.mapping = MappingService(service)
    logger.info("Gateway ready: laya=%s tasks=%s", settings.laya_base_url, len(registry.ids()))
    yield
    await app.state.jobs.shutdown()
    if owns_backend:
        await backend.aclose()


app = FastAPI(title="Laya PoC gateway", version="0.1.0", lifespan=lifespan)


def _service(request: Request) -> ClassifyService:
    return request.app.state.service


def _compile_error(exc: CompileError) -> HTTPException:
    return HTTPException(status_code=422, detail={
        "error": "compile_error", "message": str(exc), "errors": exc.errors, "status": 422})


def _backend_error(exc: BackendError) -> HTTPException:
    return HTTPException(status_code=503, detail={
        "error": "backend_unavailable", "message": f"Laya did not answer: {exc}", "status": 503})


def _batch_entries(service: ClassifyService, req: BatchRequest, tenant_id: str
                   ) -> list[tuple[CompiledSet, dict[str, Any], str]]:
    """Compile once per distinct question_id, then render each item's state."""
    compiled: dict[Optional[str], tuple[CompiledSet, Optional[str]]] = {}
    entries = []
    for item in req.items:
        question_id = item.question_id or req.question_id
        if question_id not in compiled:
            compiled[question_id] = service.compile_for(req, tenant_id, question_id, req.rotations)
        cset, bound_question = compiled[question_id]
        state = service.state_for(cset, item.text, item.survey_question or bound_question,
                                  item.metric_score, item.context)
        entries.append((cset, state, item.text))
    return entries


# ── health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    laya = await request.app.state.backend.health()
    return {
        "status": "ok" if laya.get("status") == "ok" else "degraded",
        "laya": laya,
        "tasks": len(request.app.state.registry.ids()),
        "cache_entries": len(request.app.state.service.cache),
    }


# ── classify ────────────────────────────────────────────────────────────────

@app.post("/v1/classify", response_model=ClassifyResponse)
async def classify(req: ClassifyRequest, request: Request,
                   tenant_id: str = Depends(get_tenant_id)) -> ClassifyResponse:
    service = _service(request)
    try:
        cset, bound_question = service.compile_for(req, tenant_id)
    except CompileError as exc:
        raise _compile_error(exc)
    state = service.state_for(cset, req.text, req.survey_question or bound_question,
                              req.metric_score, req.context)
    return await service.classify_one(cset, state, tenant_id, timeout_ms=req.timeout_ms,
                                      consumer=req.thresholds, text=req.text)


@app.post("/v1/classify/batch", response_model=BatchResponse)
async def classify_batch(req: BatchRequest, request: Request,
                         tenant_id: str = Depends(get_tenant_id)) -> BatchResponse:
    settings = get_settings()
    if len(req.items) > settings.batch_max_items:
        raise HTTPException(status_code=413, detail={
            "error": "too_many_items",
            "message": f"{len(req.items)} items is over the limit of {settings.batch_max_items}; use /v1/jobs.",
            "status": 413})
    service = _service(request)
    t0 = time.perf_counter()
    try:
        entries = _batch_entries(service, req, tenant_id)
        results, stats = await service.classify_batch(
            entries, tenant_id, consumer=req.thresholds, ids=[item.id for item in req.items])
    except CompileError as exc:
        raise _compile_error(exc)
    except BackendError as exc:
        raise _backend_error(exc)
    return BatchResponse(results=results, latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                         groups=stats.groups, backend_calls=stats.backend_calls,
                         cache_hits=stats.cache_hits, items=len(results))


# ── jobs ────────────────────────────────────────────────────────────────────

@app.post("/v1/jobs", response_model=JobStatus, status_code=202)
async def create_job(req: BatchRequest, request: Request,
                     tenant_id: str = Depends(get_tenant_id)) -> JobStatus:
    settings = get_settings()
    if len(req.items) > settings.job_max_items:
        raise HTTPException(status_code=413, detail={
            "error": "too_many_items",
            "message": f"{len(req.items)} items is over the limit of {settings.job_max_items}.",
            "status": 413})
    try:
        entries = _batch_entries(_service(request), req, tenant_id)
    except CompileError as exc:
        raise _compile_error(exc)
    return request.app.state.jobs.start(tenant_id, entries, [item.id for item in req.items], req.thresholds)


@app.get("/v1/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, request: Request, tenant_id: str = Depends(get_tenant_id)) -> JobStatus:
    status = request.app.state.jobs.get(job_id, tenant_id)
    if status is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No job {job_id}.", "status": 404})
    return status


# ── column mapping ──────────────────────────────────────────────────────────

@app.post("/v1/map/columns", response_model=MapColumnsResponse)
async def map_columns(req: MapColumnsRequest, request: Request,
                      tenant_id: str = Depends(get_tenant_id)) -> MapColumnsResponse:
    if req.mode == "survey_question" and not req.survey_questions:
        raise HTTPException(status_code=422, detail={
            "error": "missing_survey_questions",
            "message": "mode=survey_question needs survey_questions.", "status": 422})
    try:
        return await request.app.state.mapping.map_columns(req, tenant_id)
    except CompileError as exc:
        raise _compile_error(exc)
    except BackendError as exc:
        raise _backend_error(exc)


# ── bindings ────────────────────────────────────────────────────────────────

@app.put("/v1/bindings/{survey_no}/{question_id}", response_model=BindingSaved)
async def put_binding(survey_no: int, question_id: str, binding: Binding, request: Request,
                      tenant_id: str = Depends(get_tenant_id)) -> BindingSaved:
    if binding.survey_no != survey_no or binding.question_id != question_id:
        raise HTTPException(status_code=422, detail={
            "error": "path_mismatch",
            "message": "survey_no and question_id in the body must match the path.", "status": 422})
    service = _service(request)
    try:
        cset = compile_set(service.registry, binding=binding,
                           default_model=get_settings().laya_default_model)
    except CompileError as exc:
        raise _compile_error(exc)
    budget = cset.budget_report()
    if not budget.ok:
        # Surface the problem in the builder, not at respondent time.
        raise HTTPException(status_code=422, detail={
            "error": "over_budget",
            "message": "One or more questions do not fit the option token budget.",
            "budget": budget.model_dump(), "status": 422})
    await asyncio.to_thread(service.bindings.put, tenant_id, binding)
    return BindingSaved(binding=binding, question_hash=cset.set_hash, budget=budget)


@app.get("/v1/bindings/{survey_no}/{question_id}", response_model=Binding)
async def get_binding(survey_no: int, question_id: str, request: Request,
                      tenant_id: str = Depends(get_tenant_id)) -> Binding:
    binding = _service(request).bindings.get(tenant_id, survey_no, question_id)
    if binding is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found",
            "message": f"No binding for survey {survey_no} question {question_id}.", "status": 404})
    return binding


# ── debugging ───────────────────────────────────────────────────────────────

@app.get("/v1/tasks")
async def list_tasks(request: Request, _tenant_id: str = Depends(get_tenant_id)) -> dict[str, Any]:
    return {"tasks": [
        {"id": t.id, "type": t.type, "description": t.description, "instructions": t.instructions,
         "criteria": t.criteria, "state": t.state, "model": t.model, "thresholds": t.thresholds,
         "rotations": t.rotations}
        for t in request.app.state.registry.all()
    ]}


@app.post("/v1/tasks/compile", response_model=CompileResponse)
async def compile_tasks(req: CompileRequest, request: Request,
                        tenant_id: str = Depends(get_tenant_id)) -> CompileResponse:
    """Dry run: the exact request a call would send to laya-serve. Nothing is sent."""
    service = _service(request)
    try:
        cset, bound_question = service.compile_for(
            QuestionSelector(**req.model_dump(exclude={"rotations", "sample"})), tenant_id,
            rotations=req.rotations)
    except CompileError as exc:
        raise _compile_error(exc)
    sample = req.sample
    state: Any = "<state>"
    if sample is not None:
        state = service.state_for(cset, sample.text, sample.survey_question or bound_question,
                                  sample.metric_score, sample.context)
    laya_request: dict[str, Any] = {"state": state,
                                    "questions": cset.wire_questions(rotate=req.rotations is not None)}
    if cset.model != "auto":
        laya_request["model"] = cset.model
    return CompileResponse(model=cset.model, question_hash=cset.set_hash, state_fields=cset.state_fields,
                           budget=cset.budget_report(), laya_request=laya_request)
