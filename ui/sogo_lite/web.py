"""Shared helpers for the route modules: templates, form parsing, redirects."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from sogo_lite import db, engine, gateway, modules
from sogo_lite.config import TOKEN_ENV, gateway_token, get_settings
from sogo_lite.modules.pii import KINDS as PII_KINDS

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _pretty(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, indent=2, ensure_ascii=False)


templates.env.filters["pretty"] = _pretty
templates.env.globals.update(
    module_on=modules.is_on, MODULES=modules.MODULES, MODULE_NAMES=modules.MODULE_NAMES,
    QUESTION_TYPES=engine.QUESTION_TYPES, METRICS=engine.METRICS, REQUIRED_MODES=engine.REQUIRED_MODES,
    PROJECT_TYPES=engine.PROJECT_TYPES, TOKEN_ENV=TOKEN_ENV, PII_KINDS=PII_KINDS,
)


def render(request: Request, name: str, **context: Any) -> HTMLResponse:
    context.setdefault("msg", request.query_params.get("msg"))
    context.update(
        token_present=bool(gateway_token()), gateway_url=get_settings().gateway_base_url,
        faults_on=[f["name"] for f in gateway.FAULTS if gateway.fault_settings()[f["id"]]],
        embed=request.query_params.get("embed") == "1",
    )
    return templates.TemplateResponse(request, name, context)


async def form_data(request: Request) -> FormData:
    """Dependency: lets plain `def` handlers (which run in the threadpool) read a form."""
    return await request.form()


def redirect(url: str, msg: Optional[str] = None) -> RedirectResponse:
    if msg:
        url, _, fragment = url.partition("#")
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg) + (f"#{fragment}" if fragment else "")
    return RedirectResponse(url, status_code=303)


def back(request: Request, default: str, msg: Optional[str] = None) -> RedirectResponse:
    """Redirect to where the form was posted from."""
    target = request.headers.get("referer") or default
    target = target.split("?msg=")[0].split("&msg=")[0]
    return redirect(target, msg)


def project_or_404(survey_no: int) -> dict[str, Any]:
    proj = engine.project(survey_no)
    if not proj:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"No project {survey_no}.", "status": 404})
    return proj


def to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def unsynced_class_sets(survey_no: Optional[int] = None) -> int:
    if survey_no is None:
        return db.val("SELECT COUNT(*) FROM class_set WHERE dirty=1") or 0
    return db.val("SELECT COUNT(*) FROM class_set WHERE dirty=1 AND survey_no=?", survey_no) or 0
