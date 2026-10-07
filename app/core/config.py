"""
app/core/config.py
Centralised Pydantic Settings for the Laya PoC gateway.
All environment variables are loaded once and cached.
"""
from __future__ import annotations
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Application ───────────────────────────────────────────────────────
    app_env: str = "dev"
    debug: bool = False
    data_dir: str = "./data"
    log_dir: str = "./logs"

    # ── Backend switch ────────────────────────────────────────────────────
    # laya: self-hosted laya-serve.  jev: the TypeSafe API (external; billed per input token).
    backend: Literal["laya", "jev"] = "laya"

    # ── Laya backend ──────────────────────────────────────────────────────
    laya_base_url: str = "http://127.0.0.1:8000"
    laya_api_key: str = ""
    laya_default_model: Literal["auto", "english", "multilingual"] = "auto"

    # ── Jev backend (same variable names as typesafe-sdk) ─────────────────
    typesafe_api_key: str = ""
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_default_model: str = "jev-latest"
    # Concurrent single calls for one batch: Jev has no batch route.
    jev_max_concurrent: int = 8

    # ── Calling patterns ──────────────────────────────────────────────────
    live_timeout_ms: int = 300
    # Live calls waiting on Laya at once. Laya runs one forward pass at a time, so calls
    # past this would only queue beyond the budget; they fall back at once instead. 0 = no cap.
    live_max_inflight: int = 4
    batch_max_items: int = 256
    batch_timeout_s: float = 60.0
    job_max_items: int = 200000

    # ── Banding defaults ──────────────────────────────────────────────────
    threshold_act: float = 0.85
    threshold_suggest: float = 0.60

    # ── Cache / decision log ──────────────────────────────────────────────
    cache_max_entries: int = 200000
    log_text: bool = False             # off: the decision log stores a text hash only

    # ── JWT Authentication (RSA — same keypair as Research.Auth) ─────────
    jwt_public_key_path: str = ""      # Path to RSA public PEM file (e.g. ./keys/public.pem)
    jwt_algorithm: str = "RS256"       # Algorithm — RS256 matches Research.Auth (jose-jwt)
    dev_auth_bypass: bool = False      # DEV ONLY: treat Bearer value as raw corp_no (skip JWT verify)

    # ── Feature flags ─────────────────────────────────────────────────────
    @property
    def laya_auth_enabled(self) -> bool:
        return bool(self.laya_api_key)

    @property
    def default_thresholds(self) -> dict[str, float]:
        return {"act": self.threshold_act, "suggest": self.threshold_suggest}

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
