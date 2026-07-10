"""Governed memory-kind registry: lookup, validation, caching (ENG-AUD-010 / F17).

The ``memory_kinds`` table is the tenant-scoped source of truth for which
``memory_items.kind`` values are valid and what behavior they carry
(``singleton`` supersession, ``requires_review`` initial review status,
``stays_in_recall_when_disputed`` startup-recall inclusion). This module is
the only place that queries it — routes and the classifier call
:func:`get_enabled_memory_kinds` / :func:`require_enabled_memory_kind` rather
than hard-coding kind-name checks.

``BUILTIN_KINDS`` is *not* the runtime authority. It exists only for:
  * seeding a new tenant's registry rows (:func:`seed_builtin_kinds`), mirroring
    the seed data in ``migrations/007_memory_kinds.sql``; and
  * a defensive fallback if a tenant somehow has zero enabled registry rows
    (a broken deployment) — normal operation always reads the registry.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engram.config import settings
from engram.models import MemoryKind

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class UnknownMemoryKindError(ValueError):
    """Raised when a kind is not registered, or is disabled, for a tenant."""


@dataclass(frozen=True)
class BuiltinKindSpec:
    name: str
    display_name: str
    description: str
    singleton: bool
    stays_in_recall_when_disputed: bool
    requires_review: bool
    default_importance: float
    sort_order: int


# Kept in sync with the seed VALUES table in migrations/007_memory_kinds.sql —
# see that file's comment block for the audited behavior mapping.
BUILTIN_KINDS: tuple[BuiltinKindSpec, ...] = (
    BuiltinKindSpec(
        "fact", "Fact", "An observed or stated fact.", False, False, False, 0.5, 10
    ),
    BuiltinKindSpec(
        "preference",
        "Preference",
        "A stated preference or convention.",
        True,
        False,
        False,
        0.5,
        20,
    ),
    BuiltinKindSpec(
        "doctrine",
        "Doctrine",
        "A standing policy or rule that governs behavior.",
        False,
        True,
        True,
        0.7,
        30,
    ),
    BuiltinKindSpec(
        "decision",
        "Decision",
        "A decision that was made and should be remembered.",
        False,
        False,
        True,
        0.6,
        40,
    ),
    BuiltinKindSpec(
        "invariant",
        "Invariant",
        "A rule that must always hold; violations are high-stakes.",
        True,
        True,
        True,
        0.8,
        50,
    ),
    BuiltinKindSpec(
        "observation",
        "Observation",
        "Something noticed but not yet trusted or reviewed.",
        False,
        False,
        False,
        0.4,
        60,
    ),
    BuiltinKindSpec(
        "diary_entry", "Diary Entry", "A private agent diary entry.", False, False, False, 0.4, 70
    ),
    BuiltinKindSpec(
        "procedure",
        "Procedure",
        "A how-to, runbook, or operational procedure.",
        False,
        False,
        False,
        0.5,
        80,
    ),
    BuiltinKindSpec(
        "summary",
        "Summary",
        "A condensed summary derived from other memories.",
        False,
        False,
        False,
        0.4,
        90,
    ),
)

BUILTIN_KIND_NAMES: frozenset[str] = frozenset(spec.name for spec in BUILTIN_KINDS)

# Defensive fallback ONLY (see module docstring) — not the runtime authority.
DEFAULT_KIND_TAXONOMY: tuple[str, ...] = tuple(spec.name for spec in BUILTIN_KINDS)


async def seed_builtin_kinds(session: AsyncSession, tenant_id: UUID | str) -> None:
    """Insert any missing builtin kind rows for ``tenant_id`` (idempotent).

    Called at tenant-creation time (mirrors the migration's per-tenant seed)
    so a newly created tenant can write memory items immediately.
    """
    existing = set(
        (
            await session.execute(
                select(MemoryKind.name).where(
                    MemoryKind.tenant_id == tenant_id, MemoryKind.is_builtin.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for spec in BUILTIN_KINDS:
        if spec.name in existing:
            continue
        session.add(
            MemoryKind(
                tenant_id=tenant_id,
                name=spec.name,
                display_name=spec.display_name,
                description=spec.description,
                is_builtin=True,
                enabled=True,
                singleton=spec.singleton,
                stays_in_recall_when_disputed=spec.stays_in_recall_when_disputed,
                requires_review=spec.requires_review,
                default_importance=spec.default_importance,
                sort_order=spec.sort_order,
                created_at=now,
                updated_at=now,
            )
        )
    await session.flush()


# ---------------------------------------------------------------------------
# Per-tenant in-process TTL cache for enabled kinds, mirroring the vocab cache
# pattern in engram.classification (ENG-AUD-008 / F20). Kept independent of
# that module so engram.classification can depend on engram.memory_kinds
# without a cycle. Invalidated explicitly by the admin memory-kinds routes on
# create/update/disable.
# ---------------------------------------------------------------------------


class _CacheEntry:
    __slots__ = ("value", "fetched_at")

    def __init__(self, value: object, fetched_at: float) -> None:
        self.value = value
        self.fetched_at = fetched_at


_kinds_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
_cache_lock: asyncio.Lock = asyncio.Lock()


def _cache_expired(fetched_at: float) -> bool:
    ttl = settings.vocab_cache_ttl_seconds
    if ttl <= 0:  # 0 disables caching
        return True
    return (time.monotonic() - fetched_at) > ttl


def _cache_get(tenant_id: UUID | str) -> list[MemoryKind] | None:
    key = str(tenant_id)
    entry = _kinds_cache.get(key)
    if entry is None:
        return None
    if _cache_expired(entry.fetched_at):
        _kinds_cache.pop(key, None)
        return None
    _kinds_cache.move_to_end(key)  # LRU
    return entry.value  # type: ignore[return-value]


def _cache_put(tenant_id: UUID | str, value: list[MemoryKind]) -> None:
    key = str(tenant_id)
    max_tenants = settings.vocab_cache_max_tenants
    _kinds_cache[key] = _CacheEntry(value, time.monotonic())
    _kinds_cache.move_to_end(key)
    while len(_kinds_cache) > max_tenants:
        _kinds_cache.popitem(last=False)  # evict oldest


def invalidate_memory_kind_cache(tenant_id: UUID | str | None = None) -> None:
    """Drop cached registry rows for a tenant (or all tenants when ``None``).

    Must be called by any write path that creates/updates/disables a
    ``memory_kinds`` row (the admin routes), so classification vocab and
    write-path validation see the change immediately rather than after TTL
    expiry.
    """
    if tenant_id is None:
        _kinds_cache.clear()
    else:
        _kinds_cache.pop(str(tenant_id), None)


async def _load_enabled_kinds(session: AsyncSession, tenant_id: UUID | str) -> list[MemoryKind]:
    result = await session.execute(
        select(MemoryKind)
        .where(MemoryKind.tenant_id == tenant_id, MemoryKind.enabled.is_(True))
        .order_by(MemoryKind.sort_order.asc(), MemoryKind.name.asc())
    )
    return list(result.scalars().all())


async def get_enabled_memory_kinds(
    session: AsyncSession,
    tenant_id: UUID | str,
) -> list[MemoryKind]:
    """Return the tenant's enabled kinds (cached, TTL matches vocab cache).

    Empty only means the tenant genuinely has zero enabled kinds (e.g. every
    kind was disabled, or — in a broken deployment — none were ever seeded).
    Callers that need a non-empty taxonomy should fall back to
    :data:`DEFAULT_KIND_TAXONOMY` themselves; this function never injects the
    fallback so callers can distinguish "empty" from "unknown".
    """
    cached = _cache_get(tenant_id)
    if cached is not None:
        return cached
    async with _cache_lock:
        cached = _cache_get(tenant_id)
        if cached is not None:
            return cached
        kinds = await _load_enabled_kinds(session, tenant_id)
        _cache_put(tenant_id, kinds)
        return kinds


async def require_enabled_memory_kind(
    session: AsyncSession,
    tenant_id: UUID | str,
    kind: str,
) -> MemoryKind:
    """Return the tenant's enabled :class:`MemoryKind` row for ``kind``.

    Raises :class:`UnknownMemoryKindError` if the kind is not registered or is
    disabled for this tenant. Callers (routes) map this to a 422 — an unknown
    or disabled kind must never be silently coerced to a default.
    """
    for candidate in await get_enabled_memory_kinds(session, tenant_id):
        if candidate.name == kind:
            return candidate
    raise UnknownMemoryKindError(
        f"kind {kind!r} is not a registered, enabled kind for this tenant"
    )


async def get_singleton_kind_names(session: AsyncSession, tenant_id: UUID | str) -> set[str]:
    return {k.name for k in await get_enabled_memory_kinds(session, tenant_id) if k.singleton}


async def get_disputed_stay_kind_names(session: AsyncSession, tenant_id: UUID | str) -> set[str]:
    return {
        k.name
        for k in await get_enabled_memory_kinds(session, tenant_id)
        if k.stays_in_recall_when_disputed
    }


__all__ = [
    "BUILTIN_KINDS",
    "BUILTIN_KIND_NAMES",
    "DEFAULT_KIND_TAXONOMY",
    "NAME_PATTERN",
    "BuiltinKindSpec",
    "UnknownMemoryKindError",
    "get_disputed_stay_kind_names",
    "get_enabled_memory_kinds",
    "get_singleton_kind_names",
    "invalidate_memory_kind_cache",
    "require_enabled_memory_kind",
    "seed_builtin_kinds",
]
