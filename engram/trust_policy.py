"""Canonical tenant-configured source trust and initial review policy."""

from __future__ import annotations

from typing import Literal, get_args
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.models import TenantConfig

SourceKind = Literal[
    "manual", "import", "migration", "extraction", "sync_turn", "pre_compress", "session_end"
]

_TRUST_FALLBACKS: dict[str, tuple[float, float]] = {
    "manual_user": (0.9, 0.9),
    "manual_agent": (0.6, 0.5),
    "import": (0.8, 0.8),
    "extraction": (0.5, 0.5),
    "sync_turn": (0.4, 0.4),
    "pre_compress": (0.3, 0.3),
    "session_end": (0.35, 0.35),
}
_SOURCE_TRUST_KEYS: dict[SourceKind, tuple[str, str]] = {
    "manual": ("manual_user", "manual_agent"),
    "import": ("import", "import"),
    "migration": ("import", "import"),
    "extraction": ("extraction", "extraction"),
    "sync_turn": ("sync_turn", "sync_turn"),
    "pre_compress": ("pre_compress", "pre_compress"),
    "session_end": ("session_end", "session_end"),
}
_ACTIVE_SOURCES = frozenset({"manual", "import", "migration"})


def _trust_confidence_key(source_type: str, principal_type: str) -> tuple[str, str]:
    if source_type == "manual":
        if principal_type in ("user", "admin"):
            return "manual_user", "manual_user"
        return "manual_agent", "manual_agent"
    if source_type in ("import", "migration"):
        return "import", "import"
    return source_type, source_type


def _validate_completeness() -> None:
    if set(get_args(SourceKind)) != set(_SOURCE_TRUST_KEYS):
        raise RuntimeError("SourceKind and trust-default source mappings are inconsistent")
    keys = {key for pair in _SOURCE_TRUST_KEYS.values() for key in pair}
    missing_fallbacks = keys - set(_TRUST_FALLBACKS)
    missing_columns = {
        column
        for key in keys
        for column in (f"trust_{key}", f"confidence_{key}")
        if not hasattr(TenantConfig, column)
    }
    if missing_fallbacks or missing_columns:
        raise RuntimeError(
            f"Incomplete trust defaults: fallbacks={missing_fallbacks}, columns={missing_columns}"
        )


_validate_completeness()


async def resolve_trust_defaults(
    session: AsyncSession,
    tenant_id: UUID,
    source_type: str,
    principal_type: str,
) -> tuple[float, float, str]:
    """Return source trust, confidence, and initial review state."""
    config = (
        await session.execute(
            select(TenantConfig).where(
                TenantConfig.tenant_id == tenant_id,
                TenantConfig.active.is_(True),
            )
        )
    ).scalar_one_or_none()
    trust_key, confidence_key = _trust_confidence_key(source_type, principal_type)
    if config is None:
        source_trust, confidence = _TRUST_FALLBACKS.get(trust_key, (0.5, 0.5))
    else:
        source_trust = float(getattr(config, f"trust_{trust_key}"))
        confidence = float(getattr(config, f"confidence_{confidence_key}"))
    active = source_type in _ACTIVE_SOURCES and principal_type in {"user", "admin", "system"}
    return source_trust, confidence, "active" if active else "proposed"
