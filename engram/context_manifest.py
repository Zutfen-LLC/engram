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

> The manifest proves what Engram served and which Engram policy/version
> admitted it. It does not prove that the memory was factually true or that an
> agent relied on it.

Deterministic vs volatile boundary
----------------------------------
The manifest contains *only* deterministic served-context data. Receipt IDs,
timestamps, recall-log IDs, request/trace IDs, and any clock- or RNG-derived
value are excluded from the hashed object — they belong in a future receipt
envelope (ENG-CONTEXT-002). ``packet_hash`` is included because it is derived
purely from the served packet bytes. ``manifest_hash`` is computed *over* the
canonical manifest bytes and is never placed inside the object being hashed.

Normative wire round-trip
-------------------------
The model emits the normative wire shape (e.g. the top-level ``"schema"`` key)
and parses that exact shape back: ``ContextManifestV1.model_validate`` and
``model_validate_json`` accept the emitted wire object without renaming. The
strict parser requires canonical UUID/hash representations — it does not
normalize a noncanonical wire manifest into a different canonical object.

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
from typing import Annotated, Any, Literal, Protocol, cast

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

__all__ = [
    "CANONICALIZATION",
    "MANIFEST_CONTRACT_VERSION",
    "MEMORY_CONTEXT_VERSION",
    "PACKET_MEDIA_TYPE",
    "PACKET_RENDER_VERSION",
    "SCHEMA",
    "SCHEMA_VERSION",
    "STARTUP_MODE",
    "VISIBILITY_VALUES",
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
    "normative_manifest_schema_dict",
    "reconstruct_working_set_v1",
    "sha256_digest",
]

# ─── Contract constants ────────────────────────────────────────────────
# These strings ARE the protocol. A field addition/removal, a semantic change,
# a canonicalization change, a render change, or a number-format change
# requires an explicit contract-version decision (see docs/context-manifest-v1.md
# §Versioning). Never silently change the meaning of context-manifest-v1.
#
# Typed as Final[Literal[...]] so passing them to the Literal-backed model
# fields satisfies mypy --strict while still being usable as plain strings
# elsewhere (and as the single source of truth for the protocol values).

from typing import Final  # noqa: E402

SCHEMA: Final[Literal["engram.context-manifest"]] = "engram.context-manifest"
SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"
CANONICALIZATION: Final[Literal["rfc8785"]] = "rfc8785"
STARTUP_MODE: Final[Literal["startup"]] = "startup"

# Decision-context version strings. These mirror the runtime values used by the
# completed recall operation (see engram.recall / engram.relationship_recall).
MEMORY_CONTEXT_VERSION: Final[Literal["memory-context-v2"]] = "memory-context-v2"
MANIFEST_CONTRACT_VERSION: Final[Literal["context-manifest-v1"]] = "context-manifest-v1"
PACKET_RENDER_VERSION: Final[Literal["working-set-v1"]] = "working-set-v1"
PACKET_MEDIA_TYPE: Final[Literal["text/plain; charset=utf-8"]] = "text/plain; charset=utf-8"

# Authoritative visibility vocabulary — mirrors the storage CHECK constraint
# `chk_visibility` in migrations/001_init.sql:
#   CHECK (visibility IN ('private','workspace','tenant','public'))
VISIBILITY_VALUES: frozenset[str] = frozenset(
    {"private", "workspace", "tenant", "public"}
)

# `authority` is intentionally NOT range-constrained: the storage column is a
# SmallInteger with no CHECK range, so constraining it here would invent a
# contract inconsistent with current storage semantics.

# ─── Constrained type aliases ──────────────────────────────────────────
# Reusable aliases so Pydantic validation, the generated JSON Schema, and the
# documentation all describe identical restrictions.

# Canonical lowercase UUID string (8-4-4-4-12 hex). Stored as a string — never
# parsed-and-renormalized — so the wire parser requires the canonical form.
CanonicalUuidV1 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]

# Optional canonical UUID (null allowed).
OptionalCanonicalUuidV1 = Annotated[
    str | None,
    StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
]

# sha256:<64 lowercase hexadecimal characters>
Sha256DigestV1 = Annotated[
    str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
]

OptionalSha256DigestV1 = Annotated[
    str | None, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
]

# Nonnegative integer (counts, ordinals, byte sizes, budgets, versions).
NonNegativeIntV1 = Annotated[int, Field(ge=0)]

# Strict finite float (no NaN, no ±Infinity, no bool coercion via strict mode).
FiniteFloatV1 = Annotated[float, Field(allow_inf_nan=False)]

# Visibility enum, mirroring the storage CHECK constraint.
VisibilityV1 = Literal["private", "workspace", "tenant", "public"]


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


# ─── Working-set-v1 reconstruction (coherence check only) ──────────────


def reconstruct_working_set_v1(items: list[dict[str, Any]]) -> str:
    """Reconstruct the ``working-set-v1`` packet string from response items.

    This is used ONLY as an integrity check that ``response.working_set``
    describes the same packet as ``response.items``. The actual ``packet.hash``
    is always computed directly over ``response.working_set.encode("utf-8")`` —
    this reconstruction is never substituted as the hash preimage.

    Render contract: ``[kind] content`` lines joined with ``\\n`` and NO
    trailing newline. Exact item order, exact kind, exact content (including
    embedded newlines), LF separators, and exact Unicode/whitespace are
    preserved.
    """
    return "\n".join(f"[{item['kind']}] {item['content']}" for item in items)


# ─── Strict model base ─────────────────────────────────────────────────


class _StrictModel(BaseModel):
    """Base for every manifest model: forbid unknown fields and coerce nothing.

    ``strict=True`` rejects implicit coercions (str→int, int→bool, "false"→bool,
    str→list-of-chars, etc.) so the cryptographic contract cannot silently
    misinterpret a malformed value. ``populate_by_name=True`` allows Python
    construction by field name (e.g. ``schema_name=``) while the wire alias
    (``"schema"``) is the serialization/validation key.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )


# ─── Subject ───────────────────────────────────────────────────────────


class ContextManifestSubjectV1(_StrictModel):
    """The resolved identity and profile that admitted the served context.

    ``workspace_id`` is the *resolved authorized* workspace reference, not an
    unresolved caller slug. An unprofiled context uses null profile fields.
    Profile and revision identity must be coherent (see validator).
    """

    tenant_id: CanonicalUuidV1
    principal_id: CanonicalUuidV1
    workspace_id: OptionalCanonicalUuidV1
    memory_context_version: Literal["memory-context-v2"]
    memory_profile_id: OptionalCanonicalUuidV1
    memory_profile_revision_id: OptionalCanonicalUuidV1
    memory_profile_version: NonNegativeIntV1 | None

    @model_validator(mode="after")
    def _profile_coherence(self) -> ContextManifestSubjectV1:
        # An unprofiled context sets ALL profile fields to null together.
        # A profiled context sets id + revision + version together. This
        # forbids half-specified profiles that would make the hash ambiguous.
        profiled = [
            self.memory_profile_id is not None,
            self.memory_profile_revision_id is not None,
            self.memory_profile_version is not None,
        ]
        if any(profiled) and not all(profiled):
            raise ValueError(
                "profile fields must be all set or all null together "
                "(memory_profile_id, memory_profile_revision_id, "
                "memory_profile_version)"
            )
        return self


# ─── Request descriptor ────────────────────────────────────────────────


class ContextManifestRequestedV1(_StrictModel):
    """What the caller asked for (before server defaults applied)."""

    workspace_supplied: bool
    byte_budget: NonNegativeIntV1 | None
    token_budget: NonNegativeIntV1 | None
    item_budget: NonNegativeIntV1 | None


class ContextManifestEffectiveV1(_StrictModel):
    """What the server actually used after applying defaults and resolution.

    ``workspace_id`` is the resolved authorized workspace reference (null when
    recall is principal-scoped). Startup v1 does not enforce an item budget, so
    ``item_budget`` is always null here (the builder verifies this).
    """

    workspace_id: OptionalCanonicalUuidV1
    byte_budget: NonNegativeIntV1 | None
    token_budget: NonNegativeIntV1 | None
    item_budget: None  # always null for startup v1


class ContextManifestRequestInputV1(_StrictModel):
    """Builder input: the request descriptor *without* ``request_digest``.

    ``request_digest`` is derived by the builder over the canonical bytes of
    this object (minus the digest), so it cannot be supplied as input.
    """

    requested: ContextManifestRequestedV1
    effective: ContextManifestEffectiveV1
    # Startup query_digest is always null (startup recall has no query). A raw
    # query is never stored. Semantic mode will populate this in a future slice.
    query_digest: OptionalSha256DigestV1


class ContextManifestRequestV1(_StrictModel):
    """The request descriptor placed in the manifest, with computed digest.

    ``request_digest`` is the SHA-256 of the canonical RFC 8785 bytes of this
    object *without* the ``request_digest`` field itself.
    """

    requested: ContextManifestRequestedV1
    effective: ContextManifestEffectiveV1
    query_digest: OptionalSha256DigestV1
    request_digest: Sha256DigestV1


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
    manifest_contract_version: Literal["context-manifest-v1"]
    packet_render_version: Literal["working-set-v1"]


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

    item_count: NonNegativeIntV1
    served_content_byte_count: NonNegativeIntV1
    rendered_packet_byte_count: NonNegativeIntV1
    pinned_omitted_count: NonNegativeIntV1
    omitted_count: NonNegativeIntV1
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

    media_type: Literal["text/plain; charset=utf-8"]
    render_version: Literal["working-set-v1"]
    hash: Sha256DigestV1


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

    ordinal: NonNegativeIntV1
    item_id: CanonicalUuidV1
    kind: str
    served_content_hash: Sha256DigestV1
    review_status: str
    authority: int
    visibility: VisibilityV1
    workspace_id: OptionalCanonicalUuidV1
    score: FiniteFloatV1 | None
    reasons: list[str]
    warnings: list[str]
    pinned: bool
    importance: FiniteFloatV1
    source_trust: FiniteFloatV1
    memory_confidence: FiniteFloatV1
    human_verified: bool
    conflict_type: str | None
    conflict_resolution_status: str | None


# ─── Top-level manifest ────────────────────────────────────────────────


class ContextManifestV1(_StrictModel):
    """The canonical served-context artifact.

    ``schema``, ``schema_version``, ``canonicalization``, and ``mode`` are
    fixed protocol markers — required ``Literal`` constants on the wire. The
    Python field for ``"schema"`` is ``schema_name`` (a bidirectional alias)
    because ``schema`` collides with ``BaseModel.schema``. ``manifest_hash``
    is deliberately absent: it is computed over this object's canonical bytes
    and lives outside it.
    """

    schema_name: Literal["engram.context-manifest"] = Field(
        alias="schema", serialization_alias="schema"
    )
    schema_version: Literal["1.0"]
    canonicalization: Literal["rfc8785"]
    mode: Literal["startup"]
    subject: ContextManifestSubjectV1
    request: ContextManifestRequestV1
    versions: ContextManifestVersionsV1
    result: ContextManifestResultV1
    packet: ContextManifestPacketV1
    items: list[ContextManifestItemV1]

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


def normative_manifest_schema_dict() -> dict[str, Any]:
    """Return the normative JSON Schema (draft 2020-12) for ``ContextManifestV1``.

    This is the single source of truth for the checked-in
    ``schemas/context-manifest-v1.schema.json`` (see
    ``scripts/generate_context_manifest_schema.py``). Built from the strict wire
    model with ``by_alias=True`` so the property key is the normative
    ``"schema"`` rather than the internal ``schema_name``.
    """
    schema = ContextManifestV1.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "ContextManifestV1"
    schema["description"] = (
        "Canonical, deterministic served-context manifest (ENG-CONTEXT-001). "
        "Canonical JSON is RFC 8785; manifest_hash is SHA-256 of the canonical "
        "bytes and is NOT a field in this object. The normative wire parser "
        "requires canonical UUID/hash representations."
    )
    return schema


# ─── Finalized-response protocol ───────────────────────────────────────


class RecallResponseLike(Protocol):
    """Structural type for the finalized recall response the builder consumes.

    The builder reads the served packet (``working_set``), the ordered item
    dicts, and the response's declared counts — never a database session, ORM
    row, recall log, or repository callback. All served values originate from
    this finalized object.

    The declared ``item_count`` and ``byte_count`` are checked against values
    derived from ``items``/``working_set`` before they are trusted into the
    manifest (response coherence, ENG-CONTEXT-001 correction).
    """

    working_set: str
    item_count: int
    byte_count: int
    pinned_omitted_count: int
    omitted_count: int
    items: list[dict[str, Any]]
    message: str | None


# ─── Builder ───────────────────────────────────────────────────────────


def _require_str(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ValueError(f"{where} must be a str, got {type(value).__name__}")
    return value


def _require_int(value: Any, *, where: str) -> int:
    # bool is a subclass of int; reject it explicitly so `1` is not accepted as
    # an authority or count.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an int (not bool), got {type(value).__name__}")
    return value


def _require_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where} must be a bool, got {type(value).__name__}")
    return value


def _require_str_list(value: Any, *, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list, got {type(value).__name__}")
    for element in value:
        if not isinstance(element, str) or isinstance(element, bool):
            raise ValueError(
                f"{where} must contain only str, got element {type(element).__name__}"
            )
    return value


def _require_finite_float(value: Any, *, where: str) -> float:
    # Reject bool (subclass of int/float), then require a real number, then
    # reject NaN/Infinity.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{where} must be a number, got {type(value).__name__}")
    import math

    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"{where} must be finite (not NaN/Infinity)")
    return float(value)


def _require_optional_str(value: Any, *, where: str) -> str | None:
    if value is None:
        return None
    return _require_str(value, where=where)


def _require_visibility(value: Any, *, where: str) -> VisibilityV1:
    value = _require_str(value, where=where)
    if value not in VISIBILITY_VALUES:
        raise ValueError(
            f"{where} must be one of {sorted(VISIBILITY_VALUES)}, got {value!r}"
        )
    return cast("VisibilityV1", value)


def _validate_response_coherence(response: RecallResponseLike) -> None:
    """Prove the finalized response is internally consistent before building.

    Checks (ENG-CONTEXT-001 correction, Blocker 2):
      - declared item_count == len(items);
      - declared byte_count == sum of served content UTF-8 byte sizes;
      - working_set == the working-set-v1 render of items (exact order/kind/
        content/LF/no-trailing-newline).

    A contradictory response must not produce a cryptographically valid but
    internally false manifest. The render comparison is an integrity check for
    ``working-set-v1``; the actual ``packet.hash`` is still computed directly
    over ``response.working_set`` bytes (never over the reconstruction).
    """
    if not isinstance(response.items, list):
        raise ValueError("response.items must be a list")

    declared_count = _require_int(response.item_count, where="response.item_count")
    if declared_count != len(response.items):
        raise ValueError(
            f"response.item_count ({declared_count}) does not match "
            f"len(items) ({len(response.items)})"
        )

    declared_bytes = _require_int(response.byte_count, where="response.byte_count")
    derived_bytes = 0
    for index, raw in enumerate(response.items):
        if not isinstance(raw, dict):
            raise ValueError(f"response.items[{index}] must be a dict")
        content = raw.get("content")
        if not isinstance(content, str):
            raise ValueError(f"response.items[{index}].content must be a str")
        derived_bytes += len(content.encode("utf-8"))
    if declared_bytes != derived_bytes:
        raise ValueError(
            f"response.byte_count ({declared_bytes}) does not match derived "
            f"served-content byte count ({derived_bytes})"
        )

    reconstructed = reconstruct_working_set_v1(response.items)
    if not isinstance(response.working_set, str):
        raise ValueError("response.working_set must be a str")
    if response.working_set != reconstructed:
        raise ValueError(
            "response.working_set does not match the working-set-v1 render of "
            "response.items (item order, kind, content, LF separators, or "
            "trailing newline differ)"
        )


def _validate_startup_context_coherence(
    *,
    request_context: ContextManifestRequestInputV1,
    subject_context: ContextManifestSubjectV1,
) -> None:
    """Startup v1 subject/request invariants (ENG-CONTEXT-001 correction)."""
    if request_context.query_digest is not None:
        raise ValueError("startup request.query_digest must be null")
    if subject_context.workspace_id != request_context.effective.workspace_id:
        raise ValueError(
            "subject.workspace_id must equal request.effective.workspace_id"
        )
    if request_context.effective.item_budget is not None:
        raise ValueError(
            "startup v1 effective.item_budget must be null (startup does not "
            "enforce an item budget; do not falsely attest one)"
        )


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
    validates response coherence (counts, byte count, packet render), startup
    subject/request invariants, ordinals, and strict field types before
    constructing the manifest. Mutable post-response database state has no
    path into the returned manifest.

    Raises ``ValueError`` (or ``ValidationError``) if the response is
    internally inconsistent (count/byte/packet mismatch), the request/subject
    is incoherent for startup, or any served field is malformed or wrongly
    typed.
    """
    # Version precondition checks.
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

    # Blocker 2: response + startup-context coherence.
    _validate_response_coherence(response)
    _validate_startup_context_coherence(
        request_context=request_context, subject_context=subject_context
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
        content = _require_str(
            raw.get("content"), where=f"response.items[{ordinal}].content"
        )
        served_content_byte_count += len(content.encode("utf-8"))
        manifest_items.append(_build_item_snapshot(ordinal=ordinal, raw=raw))

    rendered_packet_byte_count = len(packet_bytes)
    result = ContextManifestResultV1(
        item_count=len(response.items),
        served_content_byte_count=served_content_byte_count,
        rendered_packet_byte_count=rendered_packet_byte_count,
        pinned_omitted_count=_require_int(
            response.pinned_omitted_count, where="response.pinned_omitted_count"
        ),
        omitted_count=_require_int(
            response.omitted_count, where="response.omitted_count"
        ),
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
        # Use the wire alias `schema=` for the protocol marker so mypy infers
        # the Literal type and the construction mirrors the wire shape. The
        # Python field name is `schema_name`; both are accepted because
        # populate_by_name=True is set.
        schema=SCHEMA,
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
    """Construct one item snapshot, validating the selected manifest fields.

    Served recall item dicts are loose ``dict[str, Any]`` and may carry extra
    additive fields; those are ignored (the manifest only selects its own
    fields). The selected values are validated exactly — strict types, no
    silent coercion. ``served_content_hash`` is recomputed from served content
    so the manifest never trusts a caller-supplied hash.
    """
    content = _require_str(raw["content"], where="item content")
    served_content_hash = sha256_digest(content.encode("utf-8"))

    return ContextManifestItemV1(
        ordinal=ordinal,
        item_id=_require_str(raw.get("id"), where="item id"),
        kind=_require_str(raw.get("kind"), where="item kind"),
        served_content_hash=served_content_hash,
        review_status=_require_str(raw.get("review_status"), where="item review_status"),
        authority=_require_int(raw.get("authority"), where="item authority"),
        visibility=_require_visibility(raw.get("visibility"), where="item visibility"),
        workspace_id=_require_optional_str(
            raw.get("workspace_id"), where="item workspace_id"
        ),
        score=(
            None
            if raw.get("score") is None
            else _require_finite_float(raw.get("score"), where="item score")
        ),
        reasons=_require_str_list(raw.get("reasons"), where="item reasons"),
        warnings=_require_str_list(raw.get("warnings"), where="item warnings"),
        pinned=_require_bool(raw.get("pinned"), where="item pinned"),
        importance=_require_finite_float(raw.get("importance"), where="item importance"),
        source_trust=_require_finite_float(
            raw.get("source_trust"), where="item source_trust"
        ),
        memory_confidence=_require_finite_float(
            raw.get("memory_confidence"), where="item memory_confidence"
        ),
        human_verified=_require_bool(raw.get("human_verified"), where="item human_verified"),
        conflict_type=_require_optional_str(
            raw.get("conflict_type"), where="item conflict_type"
        ),
        conflict_resolution_status=_require_optional_str(
            raw.get("conflict_resolution_status"),
            where="item conflict_resolution_status",
        ),
    )
