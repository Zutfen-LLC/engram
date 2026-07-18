"""Canonical, versioned Context Manifest contract (ENG-CONTEXT-001).

This module defines the deterministic artifact that lets Engram **prove what
context it served and which policy/version admitted it**. It is the foundation
for ENG-CONTEXT-002 (durable receipts) and ENG-CONTEXT-003 (inspect/verify API).

What the manifest proves
------------------------
The manifest is a deterministic, content-addressed description of a *finalized,
served* recall response. Two identical served packets, under an identical
decision context, produce byte-identical canonical JSON and therefore an
identical ``manifest_hash``.

What the manifest does NOT prove
-------------------------------
It is not a truth certificate. It does not prove that any memory was factually
true, that an agent relied on the context, or that an external party endorsed
it. Retroactive exact replay of past recalls is not supported until durable
receipts land in ENG-CONTEXT-002.

Deterministic vs volatile boundary
----------------------------------
The manifest contains *only* deterministic served-context data. Receipt IDs,
timestamps, recall-log IDs, request/trace IDs, and any clock- or RNG-derived
value are excluded from the hashed object — they belong in a future receipt
envelope (ENG-CONTEXT-002). ``packet_hash`` is included because it is derived
purely from the served packet bytes. ``manifest_hash`` is computed *over* the
canonical manifest bytes and is never placed inside the object being hashed.

Canonicalization
----------------
Canonical JSON bytes are produced with RFC 8785 (JSON Canonicalization Scheme /
JCS) semantics via the pinned ``rfc8785`` library: UTF-8, no BOM, no
insignificant whitespace, UTF-16-code-unit member ordering, ECMAScript number
serialization (``-0`` collapsed to ``0``), NaN/±Infinity rejected, array order
preserved, and no Unicode normalization. The exact Unicode scalar sequence is
preserved. ``json.dumps(sort_keys=True)`` is NOT a valid substitute (it sorts
by Unicode code point, not UTF-16 code units, and does not implement the JCS
number format).

Startup-only (ENG-CONTEXT-001)
------------------------------
Only ``mode="startup"`` is supported. A future semantic mode will be added as a
separate builder without discarding this contract.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "MANIFEST_CONTRACT_VERSION",
    "MEMORY_CONTEXT_VERSION",
    "PACKET_MEDIA_TYPE",
    "PACKET_RENDER_VERSION",
    "SCHEMA",
    "SCHEMA_VERSION",
    "STARTUP_MODE",
    "ContextManifestEffectiveV1",
    "ContextManifestItemV1",
    "ContextManifestPacketV1",
    "ContextManifestRequestInputV1",
    "ContextManifestRequestV1",
    "ContextManifestRequestedV1",
    "ContextManifestResultV1",
    "ContextManifestSubjectV1",
    "ContextManifestV1",
    "ContextManifestVersionsV1",
    "RecallResponseLike",
    "build_startup_context_manifest_v1",
    "canonical_json_bytes",
    "compute_manifest_hash",
    "sha256_digest",
]

# ─── Contract constants ────────────────────────────────────────────────
# These strings ARE the protocol. A field addition/removal, a semantic change,
# a canonicalization change, a render change, or a number-format change
# requires an explicit contract-version decision (see docs/context-manifest-v1.md
# §Versioning). Never silently change the meaning of context-manifest-v1.

SCHEMA = "engram.context-manifest"
SCHEMA_VERSION = "1.0"
CANONICALIZATION = "rfc8785"
STARTUP_MODE = "startup"

# Decision-context version strings. These mirror the runtime values used by the
# completed recall operation (see engram.recall / engram.relationship_recall).
MEMORY_CONTEXT_VERSION = "memory-context-v2"
MANIFEST_CONTRACT_VERSION = "context-manifest-v1"
PACKET_RENDER_VERSION = "working-set-v1"
PACKET_MEDIA_TYPE = "text/plain; charset=utf-8"

# sha256:<64 lowercase hexadecimal characters>
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Canonical lowercase UUID string (8-4-4-4-12 hex). The manifest stores UUIDs as
# lowercase canonical strings so the canonical bytes are stable and language
# neutral; a bare uuid.UUID would serialize differently across runtimes.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# ─── Hashing helpers ───────────────────────────────────────────────────


def sha256_digest(data: bytes) -> str:
    """Return ``sha256:<64 lowercase hex>`` for the exact bytes given.

    This is the single shared hash-format helper for the manifest contract.
    No normalization is applied — the caller's bytes are hashed verbatim so
    that any byte change (whitespace, case, line ending, trailing newline) is
    detected. The empty packet hashes as the SHA-256 of zero bytes
    (``sha256:e3b0c442...``).
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def canonical_json_bytes(value: Any) -> bytes:
    """Return RFC 8785 canonical UTF-8 bytes (no BOM) for ``value``.

    NaN and ±Infinity are rejected by ``rfc8785`` (they are not valid JSON).
    ``-0.0`` is collapsed to ``0`` per RFC 8785 §3.2.2.3. Object keys are
    ordered by UTF-16 code unit; array order is preserved.
    """
    return rfc8785.dumps(value)


def compute_manifest_hash(model: BaseModel) -> str:
    """Return ``manifest_hash`` for a manifest model.

    The hash is taken over the canonical RFC 8785 bytes of the model's
    ``model_dump(mode="json", exclude_none=False, by_alias=True)`` so that the
    ``schema_name`` field serializes under its wire alias ``"schema"`` and the
    canonical bytes match the documented normative JSON shape. All optionals
    are serialized explicitly (omitted optionals and explicit ``null`` both
    surface as JSON ``null`` in the canonical bytes, so the two are not
    ambiguous at the byte level). The model being hashed MUST NOT contain a
    ``manifest_hash`` field — that value lives outside the hashed object.
    """
    payload = model.model_dump(mode="json", exclude_none=False, by_alias=True)
    return sha256_digest(canonical_json_bytes(payload))


# ─── Strict model base ─────────────────────────────────────────────────


class _StrictModel(BaseModel):
    """Base for every manifest model: forbid unknown fields.

    Unknown-field rejection is a contract invariant — it prevents accidental
    drift and blocks callers from injecting unexpected keys into the hashed
    object.
    """

    model_config = ConfigDict(extra="forbid")


def _validate_uuid_str(value: str) -> str:
    """Accept a canonical lowercase UUID string or normalize one into it.

    ``uuid.UUID`` is used to parse so any input casing/layout is accepted, but
    the canonical lowercase form is what is stored and serialized. This keeps
    the manifest language neutral and byte stable while remaining lenient on
    input.
    """
    # Imported lazily to keep the module importable in odd environments; uuid
    # is stdlib so this is cheap.
    import uuid as _uuid

    try:
        parsed = _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"not a valid UUID: {value!r}") from exc
    return str(parsed)


def _validate_hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.match(value):
        raise ValueError(
            "hash must match 'sha256:<64 lowercase hex>'; got "
            f"{value!r}"
        )
    return value


# ─── Subject ───────────────────────────────────────────────────────────


class ContextManifestSubjectV1(_StrictModel):
    """The resolved identity and profile that admitted the served context.

    ``workspace_id`` is the *resolved authorized* workspace reference, not an
    unresolved caller slug. An unprofiled context uses null profile fields.
    Profile and revision identity must be coherent (see validator).
    """

    tenant_id: str
    principal_id: str
    workspace_id: str | None
    memory_context_version: str
    memory_profile_id: str | None
    memory_profile_revision_id: str | None
    memory_profile_version: int | None

    @field_validator("tenant_id", "principal_id")
    @classmethod
    def _uuid_fields(cls, v: str) -> str:
        return _validate_uuid_str(v)

    @field_validator("workspace_id")
    @classmethod
    def _opt_uuid_field(cls, v: str | None) -> str | None:
        return _validate_uuid_str(v) if v is not None else None

    @model_validator(mode="after")
    def _profile_coherence(self) -> ContextManifestSubjectV1:
        # An unprofiled context sets ALL profile fields to null together.
        # A profiled context sets id + revision + version together. This
        # forbids half-specified profiles that would make the hash ambiguous.
        profile_fields = (
            self.memory_profile_id,
            self.memory_profile_revision_id,
            self.memory_profile_version,
        )
        profiled = [f is not None for f in profile_fields]
        if any(profiled) and not all(profiled):
            raise ValueError(
                "profile fields must be all set or all null together "
                "(memory_profile_id, memory_profile_revision_id, "
                "memory_profile_version)"
            )
        if self.memory_profile_id is not None:
            _validate_uuid_str(self.memory_profile_id)
        if self.memory_profile_revision_id is not None:
            _validate_uuid_str(self.memory_profile_revision_id)
        return self


# ─── Request descriptor ────────────────────────────────────────────────


class ContextManifestRequestedV1(_StrictModel):
    """What the caller asked for (before server defaults applied)."""

    workspace_supplied: bool
    byte_budget: int | None
    token_budget: int | None
    item_budget: int | None


class ContextManifestEffectiveV1(_StrictModel):
    """What the server actually used after applying defaults and resolution.

    ``workspace_id`` is the resolved authorized workspace reference (null when
    recall is principal-scoped).
    """

    workspace_id: str | None
    byte_budget: int | None
    token_budget: int | None
    item_budget: int | None

    @field_validator("workspace_id")
    @classmethod
    def _opt_uuid_field(cls, v: str | None) -> str | None:
        return _validate_uuid_str(v) if v is not None else None


class ContextManifestRequestInputV1(_StrictModel):
    """Builder input: the request descriptor *without* ``request_digest``.

    ``request_digest`` is derived by the builder over the canonical bytes of
    this object (minus the digest), so it cannot be supplied as input.
    """

    requested: ContextManifestRequestedV1
    effective: ContextManifestEffectiveV1
    # Startup query_digest is always null (startup recall has no query). A raw
    # query is never stored. Semantic mode will populate this in a future slice.
    query_digest: str | None

    @field_validator("query_digest")
    @classmethod
    def _opt_hash_field(cls, v: str | None) -> str | None:
        return _validate_hash(v) if v is not None else None


class ContextManifestRequestV1(_StrictModel):
    """The request descriptor placed in the manifest, with computed digest.

    ``request_digest`` is the SHA-256 of the canonical RFC 8785 bytes of this
    object *without* the ``request_digest`` field itself.
    """

    requested: ContextManifestRequestedV1
    effective: ContextManifestEffectiveV1
    query_digest: str | None
    request_digest: str

    @field_validator("request_digest")
    @classmethod
    def _hash_field(cls, v: str) -> str:
        return _validate_hash(v)

    @field_validator("query_digest")
    @classmethod
    def _opt_hash_field(cls, v: str | None) -> str | None:
        return _validate_hash(v) if v is not None else None


def _compute_request_digest(request_input: ContextManifestRequestInputV1) -> str:
    """SHA-256 of the canonical request descriptor without ``request_digest``."""
    payload = request_input.model_dump(mode="json", exclude_none=False)
    return sha256_digest(canonical_json_bytes(payload))


# ─── Decision versions ─────────────────────────────────────────────────


class ContextManifestVersionsV1(_StrictModel):
    """Runtime version identifiers from the completed recall operation.

    Git provenance is intentionally NOT hashed here — it is not a stable
    protocol version. A future receipt envelope may carry commit SHAs.
    """

    scoring_version: str
    config_version: str
    candidate_strategy_version: str
    manifest_contract_version: str
    packet_render_version: str


# ─── Result summary ────────────────────────────────────────────────────


class ContextManifestResultV1(_StrictModel):
    """Bounded aggregate counts over the served response.

    ``served_content_byte_count`` corresponds to the recall ``byte_count``
    semantics (sum of served item content byte sizes). ``rendered_packet_byte_
    count`` is the exact UTF-8 size of ``working_set``. The two are not
    interchangeable (the packet adds ``[kind] `` prefixes and LF separators).
    Only bounded aggregate omission counts are stored — never a list of
    rejected or unauthorized candidates.
    """

    item_count: int
    served_content_byte_count: int
    rendered_packet_byte_count: int
    pinned_omitted_count: int
    omitted_count: int
    message: str | None


# ─── Packet descriptor ─────────────────────────────────────────────────


class ContextManifestPacketV1(_StrictModel):
    """The rendered packet served to the caller (``working_set``).

    The current packet is the exact ``working_set`` string:

        [kind] content
        [kind] content

    Its hash preserves item order, exact content, exact kind, LF separators,
    and the presence/absence of a trailing newline. It is computed over
    ``response.working_set`` directly — never reconstructed from item rows.
    """

    media_type: str
    render_version: str
    hash: str

    @field_validator("hash")
    @classmethod
    def _hash_field(cls, v: str) -> str:
        return _validate_hash(v)


# ─── Ordered item snapshot ─────────────────────────────────────────────


class ContextManifestItemV1(_StrictModel):
    """One served item's stable ordinal and mutable decision fields.

    Item array order is the exact response order; ``ordinal`` must equal the
    array position. ``score`` may be null for pinned items. Reason and warning
    array order is significant (never alphabetized). Full memory content is
    absent — only ``served_content_hash`` (exact UTF-8 SHA-256 of the served
    content) is stored. ``conflicts_with_item_id`` is excluded: a counterpart
    may not be independently eligible.
    """

    ordinal: int
    item_id: str
    kind: str
    served_content_hash: str
    review_status: str
    authority: int
    visibility: str
    workspace_id: str | None
    score: float | None = Field(allow_inf_nan=False)
    reasons: list[str]
    warnings: list[str]
    pinned: bool
    importance: float = Field(allow_inf_nan=False)
    source_trust: float = Field(allow_inf_nan=False)
    memory_confidence: float = Field(allow_inf_nan=False)
    human_verified: bool
    conflict_type: str | None
    conflict_resolution_status: str | None

    @field_validator("item_id")
    @classmethod
    def _uuid_field(cls, v: str) -> str:
        return _validate_uuid_str(v)

    @field_validator("workspace_id")
    @classmethod
    def _opt_uuid_field(cls, v: str | None) -> str | None:
        return _validate_uuid_str(v) if v is not None else None

    @field_validator("served_content_hash")
    @classmethod
    def _hash_field(cls, v: str) -> str:
        return _validate_hash(v)


# ─── Top-level manifest ────────────────────────────────────────────────


class ContextManifestV1(_StrictModel):
    """The canonical served-context artifact.

    ``schema``, ``schema_version``, ``canonicalization``, and ``mode`` are
    fixed protocol markers. ``manifest_hash`` is deliberately absent: it is
    computed over this object's canonical bytes and lives outside it.
    """

    # ``schema`` is a reserved attribute name on BaseModel (deprecated v1
    # .json/.schema methods), so the Python field is ``schema_name`` while the
    # serialized JSON key is ``"schema"`` — the wire contract name.
    schema_name: str = Field(default=SCHEMA, serialization_alias="schema")
    schema_version: str = Field(default=SCHEMA_VERSION)
    canonicalization: str = Field(default=CANONICALIZATION)
    mode: str = Field(default=STARTUP_MODE)
    subject: ContextManifestSubjectV1
    request: ContextManifestRequestV1
    versions: ContextManifestVersionsV1
    result: ContextManifestResultV1
    packet: ContextManifestPacketV1
    items: list[ContextManifestItemV1]

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("schema_name")
    @classmethod
    def _schema_field(cls, v: str) -> str:
        if v != SCHEMA:
            raise ValueError(f"schema must be {SCHEMA!r}; got {v!r}")
        return v

    @field_validator("schema_version")
    @classmethod
    def _schema_version_field(cls, v: str) -> str:
        if v != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}; got {v!r}")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_field(cls, v: str) -> str:
        if v != STARTUP_MODE:
            raise ValueError(
                f"only mode={STARTUP_MODE!r} is supported in ENG-CONTEXT-001; "
                f"got {v!r}"
            )
        return v

    @field_validator("canonicalization")
    @classmethod
    def _canon_field(cls, v: str) -> str:
        if v != CANONICALIZATION:
            raise ValueError(
                f"canonicalization must be {CANONICALIZATION!r}; got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _ordinal_coherence(self) -> ContextManifestV1:
        # ordinals must be 0..n-1 and match array position exactly. This blocks
        # duplicate/missing/out-of-order ordinals from corrupting the artifact.
        for i, item in enumerate(self.items):
            if item.ordinal != i:
                raise ValueError(
                    f"item ordinal mismatch at position {i}: "
                    f"ordinal={item.ordinal} (expected {i})"
                )
        return self


# ─── Builder ───────────────────────────────────────────────────────────


class RecallResponseLike(Protocol):
    """Structural type for the finalized recall response the builder consumes.

    The builder only reads ``working_set`` and ``items`` (plus optional
    ``pinned_omitted_count``/``omitted_count``/``message`` for cross-checking)
    — never a database session, ORM row, recall log, or repository callback.
    All served values originate from this finalized object.
    """

    working_set: str
    items: list[dict[str, Any]]
    pinned_omitted_count: int
    omitted_count: int
    message: str | None


def build_startup_context_manifest_v1(
    *,
    response: RecallResponseLike,
    subject_context: ContextManifestSubjectV1,
    request_context: ContextManifestRequestInputV1,
    decision_versions: ContextManifestVersionsV1,
) -> ContextManifestV1:
    """Build a deterministic ``ContextManifestV1`` from a finalized response.

    The builder derives every hashed value from ``response`` — packet hash,
    per-item served-content hashes, request digest, and result counts — and
    validates that ordinals, item count, and packet bytes are coherent with the
    finalized response. Mutable post-response database state has no path into
    the returned manifest.

    Raises ``ValueError`` if the response is internally inconsistent (item
    count mismatch, packet bytes don't match rendered items, etc.).
    """
    if subject_context.memory_context_version != MEMORY_CONTEXT_VERSION:
        raise ValueError(
            "memory_context_version mismatch: manifest contract expects "
            f"{MEMORY_CONTEXT_VERSION!r}, subject has "
            f"{subject_context.memory_context_version!r}"
        )
    if decision_versions.manifest_contract_version != MANIFEST_CONTRACT_VERSION:
        raise ValueError(
            "decision_versions.manifest_contract_version must be "
            f"{MANIFEST_CONTRACT_VERSION!r}; got "
            f"{decision_versions.manifest_contract_version!r}"
        )
    if decision_versions.packet_render_version != PACKET_RENDER_VERSION:
        raise ValueError(
            "decision_versions.packet_render_version must be "
            f"{PACKET_RENDER_VERSION!r}; got "
            f"{decision_versions.packet_render_version!r}"
        )

    # Packet hash: SHA-256 of the exact UTF-8 bytes of response.working_set.
    # The empty packet hashes as SHA-256 of zero bytes. Never reconstructed.
    packet_bytes = response.working_set.encode("utf-8")
    packet_hash = sha256_digest(packet_bytes)

    # Per-item snapshots: served_content_hash is exact UTF-8 SHA-256 of the
    # served content (NOT engram.canonicalize, which lowercases/collapses).
    manifest_items: list[ContextManifestItemV1] = []
    served_content_byte_count = 0
    for ordinal, raw in enumerate(response.items):
        if not isinstance(raw, dict):
            raise ValueError(
                f"response.items[{ordinal}] is not a dict: {type(raw).__name__}"
            )
        content = raw.get("content")
        if not isinstance(content, str):
            raise ValueError(
                f"response.items[{ordinal}].content is missing or not a str"
            )
        served_content_byte_count += len(content.encode("utf-8"))
        manifest_items.append(
            _build_item_snapshot(ordinal=ordinal, raw=raw)
        )

    rendered_packet_byte_count = len(packet_bytes)
    result = ContextManifestResultV1(
        item_count=len(response.items),
        served_content_byte_count=served_content_byte_count,
        rendered_packet_byte_count=rendered_packet_byte_count,
        pinned_omitted_count=response.pinned_omitted_count,
        omitted_count=response.omitted_count,
        message=response.message,
    )

    request_digest = _compute_request_digest(request_context)
    request = ContextManifestRequestV1(
        requested=request_context.requested,
        effective=request_context.effective,
        query_digest=request_context.query_digest,
        request_digest=request_digest,
    )

    packet = ContextManifestPacketV1(
        media_type=PACKET_MEDIA_TYPE,
        render_version=PACKET_RENDER_VERSION,
        hash=packet_hash,
    )

    manifest = ContextManifestV1(
        schema_name=SCHEMA,
        schema_version=SCHEMA_VERSION,
        canonicalization=CANONICALIZATION,
        mode=STARTUP_MODE,
        subject=subject_context,
        request=request,
        versions=decision_versions,
        result=result,
        packet=packet,
        items=manifest_items,
    )
    return manifest


def _build_item_snapshot(*, ordinal: int, raw: dict[str, Any]) -> ContextManifestItemV1:
    """Construct one item snapshot, sourcing every field from the raw item dict.

    ``served_content_hash`` is recomputed from the served ``content`` so the
    manifest never trusts a caller-supplied hash. Unknown keys in ``raw`` are
    ignored here (the response item dicts are loose ``dict[str, Any]``); the
    strict ``ContextManifestItemV1`` model still forbids unknown fields in the
    *produced* manifest.
    """
    content = raw["content"]
    if not isinstance(content, str):
        raise ValueError("item content must be a str")

    def req(key: str) -> Any:
        if key not in raw:
            raise ValueError(f"response item missing required field {key!r}")
        return raw[key]

    served_content_hash = sha256_digest(content.encode("utf-8"))

    return ContextManifestItemV1(
        ordinal=ordinal,
        item_id=req("id"),
        kind=req("kind"),
        served_content_hash=served_content_hash,
        review_status=req("review_status"),
        authority=req("authority"),
        visibility=req("visibility"),
        workspace_id=raw.get("workspace_id"),
        score=raw.get("score"),
        reasons=list(req("reasons")),
        warnings=list(req("warnings")),
        pinned=bool(req("pinned")),
        importance=req("importance"),
        source_trust=req("source_trust"),
        memory_confidence=req("memory_confidence"),
        human_verified=bool(req("human_verified")),
        conflict_type=raw.get("conflict_type"),
        conflict_resolution_status=raw.get("conflict_resolution_status"),
    )
