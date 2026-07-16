"""Admin CRUD endpoints for tenant / workspace / principal / api-key management.

All endpoints require the ``admin`` scope when auth is enabled. When auth is
disabled the default principal already carries all scopes, so these endpoints
work in dev mode without a token.
"""
# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from engram.auth import (
    ADMIN_SCOPE,
    DIGEST_ALGORITHM,
    InternalPrincipalCredentialError,
    assert_principal_credentialable,
    canonicalize_scopes,
    digest_api_key_secret,
    generate_api_key,
    get_current_principal,
    parse_api_key,
    validate_principal_name,
    validate_principal_type,
)
from engram.auth import Principal as AuthPrincipal
from engram.classification import invalidate_vocab_cache
from engram.db import get_session
from engram.memory_kinds import (
    BUILTIN_KIND_NAMES,
    NAME_PATTERN,
    invalidate_memory_kind_cache,
    seed_builtin_kinds,
)
from engram.memory_profiles import ProfileNotFoundError, validate_key_binding
from engram.models import ApiKey, MemoryKind, Tenant, TenantConfig, Workspace
from engram.models import Principal as PrincipalModel
from engram.promotion import auto_promote_proposed_memories, summarize

router = APIRouter()


# --- Request / response schemas ----------------------------------------------


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class WorkspaceCreate(BaseModel):
    tenant_id: uuid.UUID
    name: str = Field(min_length=1)
    slug: str = Field(min_length=1, max_length=255)


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    slug: str


class PrincipalCreate(BaseModel):
    # ``internal_key`` is deliberately absent — it is a server-owned field that
    # no caller-facing API may set (V2-BL-003B). Extra/unknown fields are
    # rejected by model_config so a request attempting to supply
    # ``internal_key`` is rejected by request validation.
    model_config = {"extra": "forbid"}

    tenant_id: uuid.UUID
    name: str = Field(min_length=1)
    type: str = Field(default="agent", max_length=50)


class PrincipalOut(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    type: str
    internal_key: str | None = None


class ApiKeyCreate(BaseModel):
    tenant_id: uuid.UUID
    principal_id: uuid.UUID | None = None
    scopes: list[str] = Field(default=["read", "write"])
    label: str | None = None
    memory_profile_id: uuid.UUID | None = None

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: list[str]) -> list[str]:
        # Validates, dedupes, and canonically orders — the single source of
        # truth shared with `engram bootstrap-key --scopes` (V2-BL-004).
        # An unknown scope (including a typo like "reviews") raises here,
        # which Pydantic turns into a 422 rather than silently persisting it.
        return canonicalize_scopes(v)


class ApiKeyOut(BaseModel):
    """Returned only once at creation — includes the plaintext key."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    principal_id: uuid.UUID | None
    scopes: list[str]
    label: str | None
    memory_profile_id: uuid.UUID | None = None
    memory_profile_revision_id: uuid.UUID | None = None
    memory_profile_slug: str | None = None
    memory_profile_version: int | None = None
    key: str  # plaintext, shown once


class MemoryKindCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    singleton: bool = False
    stays_in_recall_when_disputed: bool = False
    requires_review: bool = False
    auto_promote_from_inferred: bool = False
    default_importance: float | None = Field(default=None, ge=0.0, le=1.0)
    sort_order: int = 100


class MemoryKindPatch(BaseModel):
    display_name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    singleton: bool | None = None
    stays_in_recall_when_disputed: bool | None = None
    requires_review: bool | None = None
    auto_promote_from_inferred: bool | None = None
    default_importance: float | None = Field(default=None, ge=0.0, le=1.0)
    sort_order: int | None = None


class MemoryKindOut(BaseModel):
    tenant_id: uuid.UUID
    name: str
    display_name: str
    description: str | None
    is_builtin: bool
    enabled: bool
    singleton: bool
    stays_in_recall_when_disputed: bool
    requires_review: bool
    auto_promote_from_inferred: bool
    default_importance: float | None
    sort_order: int


def _kind_to_out(kind: MemoryKind) -> MemoryKindOut:
    return MemoryKindOut(
        tenant_id=kind.tenant_id,
        name=kind.name,
        display_name=kind.display_name,
        description=kind.description,
        is_builtin=kind.is_builtin,
        enabled=kind.enabled,
        singleton=kind.singleton,
        stays_in_recall_when_disputed=kind.stays_in_recall_when_disputed,
        requires_review=kind.requires_review,
        auto_promote_from_inferred=kind.auto_promote_from_inferred,
        default_importance=kind.default_importance,
        sort_order=kind.sort_order,
    )


class PromotionCandidateResponse(BaseModel):
    item_id: uuid.UUID
    would_promote: bool
    selected_basis: Literal["legacy_confidence", "retention_evidence"] | None
    blockers: list[str]
    legacy_confidence: float
    legacy_threshold: float
    evidence_score: float | None
    evidence_threshold: float
    taxonomy_confidence: float | None
    retention_disposition: str | None
    classification_run_id: uuid.UUID | None
    cooling_period_start: datetime | None
    eligible_at: datetime | None
    legacy_eligible_at: datetime
    evidence_cooling_period_start: datetime | None
    evidence_eligible_at: datetime | None
    kind: str
    kind_auto_promote_allowed: bool
    conflict_recheck_status: str


class PromotionResponse(BaseModel):
    """Result of running auto-promotion Path A for the caller's tenant."""

    tenant_id: str
    enabled: bool
    confidence_threshold: float
    min_age_hours: int
    evidence_enabled: bool = False
    evidence_threshold: float = 0.70
    dry_run: bool = False
    scanned: int = 0
    promoted: int = 0
    skipped_confidence: int = 0
    skipped_age: int = 0
    skipped_conflict: int = 0
    skipped_disabled: int = 0
    skipped_dispute: int = 0
    skipped_conflict_recheck: int = 0
    skipped_kind_policy: int = 0
    skipped_evidence_disabled: int = 0
    skipped_no_retention_evidence: int = 0
    skipped_missing_source_prior: int = 0
    skipped_retention_disposition: int = 0
    skipped_taxonomy_confidence: int = 0
    skipped_evidence_score: int = 0
    skipped_evidence_version: int = 0
    skipped_evidence_inconsistent: int = 0
    skipped_review_policy: int = 0
    promoted_ids: list[uuid.UUID] = Field(default_factory=list)
    promoted_legacy_confidence: int = 0
    promoted_retention_evidence: int = 0
    would_promote: int = 0
    would_promote_legacy_confidence: int = 0
    would_promote_retention_evidence: int = 0
    would_promote_ids: list[uuid.UUID] = Field(default_factory=list)
    candidates: list[PromotionCandidateResponse] = Field(default_factory=list)
    summary: str


# --- Endpoints ---------------------------------------------------------------


@router.post(
    "/admin/tenants",
    response_model=TenantOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def create_tenant(
    body: TenantCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TenantOut:
    tenant = Tenant(name=body.name, slug=body.slug, created_at=datetime.now(UTC))
    session.add(tenant)
    await session.flush()
    # Seed the builtin kind registry (ENG-AUD-010 / F17) so the new tenant can
    # write memory items immediately — memory_items.kind is now FK-governed by
    # memory_kinds, so a tenant with zero registry rows could write nothing.
    await seed_builtin_kinds(session, tenant.id)
    # Unlike legacy rows upgraded by migration 016, a newly created tenant is
    # intentionally enrolled in the evidence lane by its real config row.
    config = await session.scalar(
        select(TenantConfig).where(
            TenantConfig.tenant_id == tenant.id, TenantConfig.active.is_(True)
        )
    )
    if config is None:
        session.add(
            TenantConfig(tenant_id=tenant.id, active=True, auto_promote_evidence_enabled=True)
        )
    await session.commit()
    await session.refresh(tenant)
    return TenantOut(id=tenant.id, name=tenant.name, slug=tenant.slug)


@router.post(
    "/admin/workspaces",
    response_model=WorkspaceOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def create_workspace(
    body: WorkspaceCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkspaceOut:
    ws = Workspace(
        tenant_id=body.tenant_id,
        name=body.name,
        slug=body.slug,
        created_at=datetime.now(UTC),
    )
    session.add(ws)
    await session.commit()
    await session.refresh(ws)
    return WorkspaceOut(id=ws.id, tenant_id=ws.tenant_id, name=ws.name, slug=ws.slug)


@router.post(
    "/admin/principals",
    response_model=PrincipalOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def create_principal(
    body: PrincipalCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PrincipalOut:
    # Reject names using the reserved internal prefix so an administrator
    # cannot create a row that impersonates a server-owned internal actor
    # (V2-BL-003B). Ordinary names including "system" remain allowed — the
    # name is no longer the security identity.
    try:
        validate_principal_name(body.name)
        validate_principal_type(body.type)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    principal = PrincipalModel(
        tenant_id=body.tenant_id,
        name=body.name,
        type=body.type,
        created_at=datetime.now(UTC),
    )
    session.add(principal)
    await session.commit()
    await session.refresh(principal)
    return PrincipalOut(
        id=principal.id,
        tenant_id=principal.tenant_id,
        name=principal.name,
        type=principal.type,
        internal_key=principal.internal_key,
    )


@router.post(
    "/admin/api-keys",
    response_model=ApiKeyOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def create_api_key(
    body: ApiKeyCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
    caller: AuthPrincipal = Depends(get_current_principal),  # noqa: B008
) -> ApiKeyOut:
    # Reject key issuance for internal (non-credentialable) principals
    # (V2-BL-003B). The validation resolves the principal inside the caller's
    # tenant context; a cross-tenant internal principal ID is not disclosed.
    if body.principal_id is not None:
        try:
            await assert_principal_credentialable(
                session,
                tenant_id=body.tenant_id,
                principal_id=body.principal_id,
            )
        except InternalPrincipalCredentialError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="API keys cannot be issued for this principal",
            ) from exc
    try:
        active_profile = await validate_key_binding(session, body.tenant_id, body.memory_profile_id)
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="memory profile not found") from exc
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None  # new-format keys always carry a key_id
    api_key = ApiKey(
        tenant_id=body.tenant_id,
        principal_id=body.principal_id,
        memory_profile_id=body.memory_profile_id,
        key_hash=None,
        key_id=parsed.key_id,
        secret_digest=digest_api_key_secret(parsed.secret),
        digest_algorithm=DIGEST_ALGORITHM,
        scopes=body.scopes,
        label=body.label,
        created_at=datetime.now(UTC),
    )
    session.add(api_key)
    await session.flush()
    if active_profile is not None:
        await session.execute(
            text(
                "INSERT INTO memory_profile_events "
                "(tenant_id, profile_id, revision_id, actor_principal_id, event_type, reason, details) "
                "VALUES (:tenant_id, :profile_id, :revision_id, :actor_principal_id, "
                "'profile_bound_at_key_issuance', 'API key issuance', "
                "jsonb_build_object('api_key_id', :key_id, 'label', :label))"
            ),
            {"tenant_id": str(body.tenant_id), "profile_id": str(active_profile.id),
             "revision_id": str(active_profile.revision_id), "key_id": str(api_key.id),
             "label": body.label, "actor_principal_id": caller.principal_id},
        )
    await session.commit()
    await session.refresh(api_key)
    return ApiKeyOut(
        id=api_key.id,
        tenant_id=api_key.tenant_id,
        principal_id=api_key.principal_id,
        scopes=list(api_key.scopes),
        label=api_key.label,
        memory_profile_id=body.memory_profile_id,
        memory_profile_revision_id=active_profile.revision_id if active_profile else None,
        memory_profile_slug=active_profile.slug if active_profile else None,
        memory_profile_version=active_profile.version if active_profile else None,
        key=plaintext,
    )


# A read-only helper used by tests and tooling to list a tenant's principals.
@router.get(
    "/admin/principals",
    response_model=list[PrincipalOut],
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def list_principals(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[PrincipalOut]:
    result = await session.execute(
        select(PrincipalModel).where(PrincipalModel.tenant_id == tenant_id)
    )
    return [
        PrincipalOut(
            id=p.id,
            tenant_id=p.tenant_id,
            name=p.name,
            type=p.type,
            internal_key=p.internal_key,
        )
        for p in result.scalars()
    ]


async def _resolve_tenant_id(session: AsyncSession) -> str:
    """Read tenant_id from RLS session context (mirrors review.py)."""
    tid_str = (
        await session.execute(text("SELECT current_setting('app.tenant_id', true)"))
    ).scalar()
    if not tid_str:
        # With RLS configured every request sets this; reaching here means the
        # session was constructed without the dependency.
        raise HTTPException(status_code=403, detail="no tenant context")
    return str(tid_str)


# --- Memory-kind registry admin (ENG-AUD-010 / F17) --------------------------
#
# Actual deletion is unsupported by design (disabling is sufficient — see the
# module-level docstring and docs/design.md). There is no DELETE endpoint, so
# "built-in kinds cannot be deleted" and "kinds referenced by existing
# memories cannot be deleted" hold trivially: nothing can ever be deleted.
# ``name`` is immutable after creation (not a PATCH field) — enforces "custom
# kinds cannot be renamed."


@router.get(
    "/admin/memory-kinds",
    response_model=list[MemoryKindOut],
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def list_memory_kinds(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[MemoryKindOut]:
    """List every kind (enabled and disabled) governed for the caller's tenant."""
    tenant_id = await _resolve_tenant_id(session)
    result = await session.execute(
        select(MemoryKind)
        .where(MemoryKind.tenant_id == tenant_id)
        .order_by(MemoryKind.sort_order.asc(), MemoryKind.name.asc())
    )
    return [_kind_to_out(k) for k in result.scalars().all()]


@router.post(
    "/admin/memory-kinds",
    response_model=MemoryKindOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def create_memory_kind(
    body: MemoryKindCreate,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MemoryKindOut:
    """Create a governed tenant custom kind.

    Names use a stable normalized format (``^[a-z][a-z0-9_]{0,63}$``) and may
    not collide with a reserved built-in kind name. Custom kinds always start
    ``is_builtin=False`` and are otherwise indistinguishable from built-ins to
    the write/classification paths — they cannot bypass review, trust, or RLS.
    """
    tenant_id = uuid.UUID(await _resolve_tenant_id(session))
    if not NAME_PATTERN.match(body.name):
        raise HTTPException(
            status_code=422,
            detail="name must match ^[a-z][a-z0-9_]{0,63}$ (lowercase snake_case)",
        )
    if body.name in BUILTIN_KIND_NAMES:
        raise HTTPException(
            status_code=422, detail=f"{body.name!r} is a reserved built-in kind name"
        )
    kind = MemoryKind(
        tenant_id=tenant_id,
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        is_builtin=False,
        enabled=True,
        singleton=body.singleton,
        stays_in_recall_when_disputed=body.stays_in_recall_when_disputed,
        requires_review=body.requires_review,
        auto_promote_from_inferred=body.auto_promote_from_inferred,
        default_importance=body.default_importance,
        sort_order=body.sort_order,
    )
    session.add(kind)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail=f"kind {body.name!r} already exists for this tenant"
        ) from exc
    await session.commit()
    invalidate_memory_kind_cache(tenant_id)
    invalidate_vocab_cache(tenant_id)
    return _kind_to_out(kind)


@router.patch(
    "/admin/memory-kinds/{name}",
    response_model=MemoryKindOut,
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def update_memory_kind(
    name: str,
    body: MemoryKindPatch,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MemoryKindOut:
    """Edit description/display name/behavior flags, or enable/disable a kind.

    Disabling a kind prevents new writes/classification into it (the write
    path and classifier vocab both read ``enabled=True`` rows only) but does
    not touch existing memories of that kind — they remain fully readable.
    """
    tenant_id = uuid.UUID(await _resolve_tenant_id(session))
    kind = (
        await session.execute(
            select(MemoryKind).where(MemoryKind.tenant_id == tenant_id, MemoryKind.name == name)
        )
    ).scalar_one_or_none()
    if kind is None:
        raise HTTPException(status_code=404, detail=f"kind {name!r} not found")

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(kind, field, value)
    kind.updated_at = datetime.now(UTC)

    await session.commit()
    invalidate_memory_kind_cache(tenant_id)
    invalidate_vocab_cache(tenant_id)
    return _kind_to_out(kind)


@router.post(
    "/admin/promote",
    response_model=PromotionResponse,
    dependencies=[Depends(ADMIN_SCOPE)],
)
async def promote_proposed(
    dry_run: bool = False,
    limit: int | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PromotionResponse:
    """Run auto-promotion Path A for the caller's tenant.

    Evaluates independent legacy-confidence and server-attested retention-
    evidence lanes, followed by shared kind, age, liveness, dispute, review-
    policy, and conflict gates. ``dry_run=true`` performs an exact state-free
    preview. A no-body request preserves actual-promotion behavior.
    """
    tenant_id = await _resolve_tenant_id(session)
    result = await auto_promote_proposed_memories(
        session, tenant_id, source="admin_endpoint", dry_run=dry_run, limit=limit
    )
    return PromotionResponse(
        tenant_id=result.tenant_id,
        enabled=result.enabled,
        confidence_threshold=result.confidence_threshold,
        min_age_hours=result.min_age_hours,
        evidence_enabled=result.evidence_enabled,
        evidence_threshold=result.evidence_threshold,
        dry_run=result.dry_run,
        scanned=result.scanned,
        promoted=result.promoted,
        skipped_confidence=result.skipped_confidence,
        skipped_age=result.skipped_age,
        skipped_conflict=result.skipped_conflict,
        skipped_disabled=result.skipped_disabled,
        skipped_dispute=result.skipped_dispute,
        skipped_conflict_recheck=result.skipped_conflict_recheck,
        skipped_kind_policy=result.skipped_kind_policy,
        skipped_evidence_disabled=result.skipped_evidence_disabled,
        skipped_no_retention_evidence=result.skipped_no_retention_evidence,
        skipped_missing_source_prior=result.skipped_missing_source_prior,
        skipped_retention_disposition=result.skipped_retention_disposition,
        skipped_taxonomy_confidence=result.skipped_taxonomy_confidence,
        skipped_evidence_score=result.skipped_evidence_score,
        skipped_evidence_version=result.skipped_evidence_version,
        skipped_evidence_inconsistent=result.skipped_evidence_inconsistent,
        skipped_review_policy=result.skipped_review_policy,
        promoted_ids=result.promoted_ids,
        promoted_legacy_confidence=result.promoted_legacy_confidence,
        promoted_retention_evidence=result.promoted_retention_evidence,
        would_promote=result.would_promote,
        would_promote_legacy_confidence=result.would_promote_legacy_confidence,
        would_promote_retention_evidence=result.would_promote_retention_evidence,
        would_promote_ids=result.would_promote_ids,
        candidates=[
            PromotionCandidateResponse(**candidate.__dict__) for candidate in result.candidates
        ],
        summary=summarize(result),
    )
