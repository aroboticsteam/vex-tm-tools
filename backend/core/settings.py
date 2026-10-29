"""Application settings, loaded from environment variables.

This module owns nothing but configuration. Per Appendix C.2, `backend/core/`
must not import from anything else in `backend/` — stdlib + third-party only.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central settings object. See plan §15 and Appendix B.1 for the full env
    var contract."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── Core infra ──────────────────────────────────────────────────────
    POSTGRES_DSN: str = Field(
        default="postgresql+asyncpg://qecomp:qecomp@localhost:5433/qecomp",
        description="Async SQLAlchemy Postgres DSN (asyncpg driver).",
    )
    REDIS_URL: str = Field(
        default="redis://localhost:6380/0",
        description=(
            "Single Redis endpoint (per Appendix A.9, HAProxy/infra handles "
            "Sentinel failover externally; the app treats this as one Redis)."
        ),
    )

    # ── OIDC (Authentik) ────────────────────────────────────────────────
    OIDC_ISSUER_URL: str = Field(default="")
    OIDC_CLIENT_ID: str = Field(default="")
    OIDC_CLIENT_SECRET: str = Field(default="")
    OIDC_GROUPS_CLAIM: str = Field(default="groups")

    # ── Local emergency admin ───────────────────────────────────────────
    ADMIN_LOCAL_PASSWORD: str = Field(
        default="",
        description="Break-glass admin password. Required to enable /admin_login.",
    )

    # ── Secrets ──────────────────────────────────────────────────────────
    SECRET_KEY: str = Field(
        default="",
        description="Signs session cookies + prompter HMAC tokens. Required.",
    )
    ENCRYPTION_KEY: str = Field(
        default="",
        description="Fernet key (32-byte base64url). Required in production.",
    )

    # ── S3 / MinIO (also settings-page-driven per §3.8; env vars are the
    #    local-dev / bootstrap fallback) ─────────────────────────────────
    S3_ENDPOINT_URL: str | None = Field(default=None)
    S3_BUCKET: str | None = Field(default=None)
    S3_ACCESS_KEY: str | None = Field(default=None)
    S3_SECRET_KEY: str | None = Field(default=None)
    S3_REGION: str = Field(default="us-east-1")
    S3_PUBLIC_ENDPOINT_URL: str | None = Field(
        default=None,
        description=(
            "Browser-reachable S3 endpoint used only for presigned URLs. Falls back to "
            "S3_ENDPOINT_URL when unset, which is correct for public AWS S3 but wrong for a "
            "self-hosted/internal endpoint (e.g. MinIO's in-cluster hostname) that a browser "
            "can't resolve."
        ),
    )

    # ── App / cluster ────────────────────────────────────────────────────
    APP_PORT: int = Field(default=8000)
    POD_IP: str | None = Field(
        default=None,
        description="Injected via k8s downward API. Falls back to local IP detection.",
    )
    # ── Web Push (VAPID) — team/staff notifications ─────────────────────
    VAPID_PRIVATE_KEY: str = Field(
        default="",
        description=(
            "VAPID private key (urlsafe-base64 raw key; see tools/generate_vapid_keys.py). "
            "Web Push is disabled while this or VAPID_PUBLIC_KEY is unset; room-display "
            "alerts still work."
        ),
    )
    VAPID_PUBLIC_KEY: str = Field(default="", description="VAPID public key (urlsafe-base64).")
    VAPID_SUBJECT: str = Field(
        default="mailto:admin@example.com",
        description="`sub` claim for VAPID — a mailto: or https: contact for the push services.",
    )
    PUBLIC_BASE_URL: str = Field(
        default="",
        description=(
            "Externally reachable base URL (e.g. https://tm.example.org) encoded into room QR "
            "codes. Falls back to the request's own base URL when unset."
        ),
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        description=(
            "Root logger level (DEBUG/INFO/WARNING/ERROR/CRITICAL). Without this "
            "configured, Python's root logger defaults to WARNING and every "
            "`logger.info(...)` call in the app — leader promotion/demotion, "
            "integration status transitions, WS reconnect messages, etc. — is "
            "silently discarded."
        ),
    )


@lru_cache
def _get_settings() -> Settings:
    return Settings()


settings = _get_settings()
