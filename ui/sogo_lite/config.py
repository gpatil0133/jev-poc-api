"""Settings for the mock: ui/config.json plus a few environment overrides.

The gateway token is read from the environment only (plan section 7.4). It is
never written to the config file, the database or the call log.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

UI_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = UI_ROOT / "config.json"

TOKEN_ENV = "SOGO_LITE_GATEWAY_TOKEN"
URL_ENV = "SOGO_LITE_GATEWAY_URL"
DB_ENV = "SOGO_LITE_DB"

# Call types and their starting timeouts (plan section 7.2). Tuned during testing.
DEFAULT_TIMEOUTS_MS = {"respondent": 300, "submit": 2000, "author": 1500, "batch": 30000}


@dataclass(frozen=True)
class Settings:
    gateway_base_url: str = "http://192.168.0.171:8010"
    db_path: str = "data/sogo_lite.db"
    http_margin_ms: int = 250
    timeouts_ms: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TIMEOUTS_MS))

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else UI_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    raw: dict = {}
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    timeouts = {**DEFAULT_TIMEOUTS_MS, **{k: int(v) for k, v in (raw.get("timeouts_ms") or {}).items()}}
    return Settings(
        gateway_base_url=(os.environ.get(URL_ENV) or raw.get("gateway_base_url")
                          or Settings.gateway_base_url).rstrip("/"),
        db_path=os.environ.get(DB_ENV) or raw.get("db_path") or Settings.db_path,
        http_margin_ms=int(raw.get("http_margin_ms", 250)),
        timeouts_ms=timeouts,
    )


def gateway_token() -> str:
    """Read on every call so a token set after start-up is picked up on restart only
    by design: the process environment is the single source."""
    return (os.environ.get(TOKEN_ENV) or "").strip()
