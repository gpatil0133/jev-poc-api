"""RS256 JWT authentication for the Laya PoC gateway.

Trimmed copy of Research.UserGuideApi app/core/auth.py (same claims contract as
survey-chat and Research.Chatbot):

  - Required on every endpoint except /health.
  - DEV_AUTH_BYPASS=true is the local-dev escape hatch: the Bearer value is a
    literal numeric corp_no. assert_production_safety() forbids it when APP_ENV
    is not dev-like.
  - `tenant_id` comes ONLY from the verified JWT claim.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import jwt as pyjwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# `auto_error=False` so we raise our own structured 401 detail body.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT issued by Research.Auth. Contains `corp_no` (or `corp_no_um`) claim.",
)


def _normalize_tenant_id(value: object) -> str:
    """Return a normalised numeric tenant id string (or empty if invalid)."""
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    if not text or not text.isdigit():
        return ""
    return str(int(text))


def _corp_no_from_jwt_payload(payload: dict) -> str:
    """Extract corp_no from JWT claims. First non-empty wins."""
    for key in ("corp_no", "corporate_no", "corp_no_um", "corpNo", "corporateNo"):
        tenant_id = _normalize_tenant_id(payload.get(key))
        if tenant_id:
            return tenant_id
    return ""


class TokenClaims:
    """Verified-JWT wrapper — mirrors Research.Chatbot req.tokenPayload."""

    __slots__ = ("tenant_id", "corp_no", "corp_no_um", "is_sub_user", "sub_corp_no", "raw")

    def __init__(self, payload: dict):
        self.raw = payload
        self.corp_no = _normalize_tenant_id(payload.get("corp_no"))
        self.corp_no_um = _normalize_tenant_id(payload.get("corp_no_um"))
        self.tenant_id = _corp_no_from_jwt_payload(payload)

        # parent != "0" AND corp_no != corp_no_um ⇒ this token is for a sub-user.
        parent = str(payload.get("parent", "0"))
        self.is_sub_user = parent != "0" and self.corp_no != self.corp_no_um
        self.sub_corp_no = self.corp_no_um if self.is_sub_user else None


_jwt_public_key: Optional[str] = None


def _get_jwt_public_key() -> str:
    """Load RSA public key PEM from disk (cached for the process lifetime)."""
    global _jwt_public_key
    if _jwt_public_key is not None:
        return _jwt_public_key

    settings = get_settings()
    key_path = settings.jwt_public_key_path
    if not key_path:
        raise HTTPException(
            status_code=500,
            detail={"error": "server_config", "message": "JWT_PUBLIC_KEY_PATH not configured.", "status": 500},
        )
    try:
        _jwt_public_key = Path(key_path).read_text(encoding="utf-8")
        logger.info("Loaded JWT public key from %s", key_path)
        return _jwt_public_key
    except Exception as exc:
        logger.error("Failed to read JWT public key from %s: %s", key_path, exc)
        raise HTTPException(
            status_code=500,
            detail={"error": "server_config", "message": f"Cannot read JWT public key: {exc}", "status": 500},
        )


def _verify_jwt(token: str) -> TokenClaims:
    """Verify a JWT signature with the RSA public key (RS256).

    Manual exp parse because Research.Auth (jose-jwt) encodes exp as a
    string-float that PyJWT's strict int check rejects.
    """
    public_key = _get_jwt_public_key()
    settings = get_settings()

    try:
        payload = pyjwt.decode(
            token,
            public_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_iat": False, "verify_exp": False},
        )
        raw_exp = payload.get("exp")
        if raw_exp is not None:
            try:
                if time.time() > float(raw_exp):
                    raise pyjwt.ExpiredSignatureError("Token has expired")
            except (ValueError, TypeError):
                # Unparseable exp → treat as non-expiring (matches Research.Chatbot).
                pass
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": "token_expired", "message": "Token has expired. Please re-authenticate.", "status": 401},
        )
    except pyjwt.InvalidTokenError as exc:
        logger.warning("JWT validation failed: %s", exc)
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_token", "message": "Invalid or malformed token.", "status": 401},
        )

    claims = TokenClaims(payload)
    if not claims.tenant_id:
        raise HTTPException(
            status_code=401,
            detail={"error": "missing_claim", "message": "Token is valid but contains no corp_no claim.", "status": 401},
        )
    return claims


def get_token_claims(
    _credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> TokenClaims:
    """Verify the Bearer JWT and return TokenClaims."""
    settings = get_settings()

    if not _credentials or not _credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "auth_required",
                "message": "Authentication required — provide Authorization: Bearer {jwt_token}.",
                "status": 401,
            },
        )
    raw = _credentials.credentials.strip()

    # DEV ONLY: skip verification, accept a numeric corp_no.
    if settings.dev_auth_bypass:
        tenant_id = _normalize_tenant_id(raw)
        if not tenant_id:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "invalid_token",
                    "message": "DEV bypass: Bearer value must be a numeric corp_no.",
                    "status": 401,
                },
            )
        return TokenClaims({"corp_no": tenant_id, "corp_no_um": tenant_id})

    return _verify_jwt(raw)


def get_tenant_id(claims: TokenClaims = Depends(get_token_claims)) -> str:
    """Convenience — return just the tenant_id from the verified JWT."""
    return claims.tenant_id


def _is_dev_like_env(app_env: str) -> bool:
    """Treat `dev`, bare `local`, and any `<env>-local` suffix as dev."""
    e = app_env.lower()
    return e == "dev" or e == "local" or e.endswith("-local")


def assert_production_safety() -> None:
    """Refuse to start with DEV_AUTH_BYPASS=true outside a dev-like APP_ENV."""
    settings = get_settings()
    if settings.dev_auth_bypass and not _is_dev_like_env(settings.app_env):
        raise RuntimeError(
            "DEV_AUTH_BYPASS=true is forbidden outside a dev-like APP_ENV "
            f"(current APP_ENV={settings.app_env!r}). Set DEV_AUTH_BYPASS=false."
        )
    if settings.dev_auth_bypass:
        logger.warning("DEV_AUTH_BYPASS active — Bearer values are accepted as raw corp_no")
