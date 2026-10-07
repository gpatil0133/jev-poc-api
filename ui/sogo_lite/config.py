"""Settings for the mock: the repo's .env (shared with the gateway), then ui/config.json.

A value set in the process environment wins over .env, and .env wins over config.json.
The mock talks to the gateway only, so the model behind it is whatever BACKEND that
gateway was started with.

The gateway token is read from the environment or .env only (plan section 7.4). It
is never written to the config file, the database or the call log.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

UI_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = UI_ROOT / "config.json"
ENV_PATH = UI_ROOT.parent / ".env"

TOKEN_ENV = "SOGO_LITE_GATEWAY_TOKEN"
URL_ENV = "SOGO_LITE_GATEWAY_URL"
DB_ENV = "SOGO_LITE_DB"

# The gateway started from this repo (`uvicorn app.api.main:app --port 8010`).
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8010"
# Call types and their starting timeouts (plan section 7.2). Tuned during testing.
DEFAULT_TIMEOUTS_MS = {"respondent": 300, "submit": 2000, "author": 1500, "batch": 30000}


@dataclass(frozen=True)
class Settings:
    gateway_base_url: str = DEFAULT_GATEWAY_URL
    gateway_url_source: str = "default"
    db_path: str = "data/sogo_lite.db"
    http_margin_ms: int = 250
    timeouts_ms: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS_MS))

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else UI_ROOT / path


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, Optional[str]]:
    return dict(dotenv_values(ENV_PATH)) if ENV_PATH.exists() else {}


def _env(name: str) -> tuple[str, str]:
    """(value, where it came from). A variable set in the process, even to empty, hides .env."""
    if name in os.environ:
        return os.environ[name].strip(), "environment"
    return (_dotenv().get(name) or "").strip(), ".env"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    raw: dict = {}
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    timeouts = {**DEFAULT_TIMEOUTS_MS, **{k: int(v) for k, v in (raw.get("timeouts_ms") or {}).items()}}
    url, source = _env(URL_ENV)
    if not url:
        url, source = ((raw["gateway_base_url"], "config.json") if raw.get("gateway_base_url")
                       else (DEFAULT_GATEWAY_URL, "default"))
    return Settings(
        gateway_base_url=url.rstrip("/"),
        gateway_url_source=source,
        db_path=_env(DB_ENV)[0] or raw.get("db_path") or Settings.db_path,
        http_margin_ms=int(raw.get("http_margin_ms", 250)),
        timeouts_ms=timeouts,
    )


def gateway_token() -> str:
    """The process environment is read on every call; .env is read once, so a token
    added there is picked up on restart."""
    return _env(TOKEN_ENV)[0]
