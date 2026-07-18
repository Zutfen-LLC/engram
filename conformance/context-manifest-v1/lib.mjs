// Shared ENG-CONTEXT-001 contract library for the JavaScript conformance
// runners. Node standard library only (no `npm install`).
//
// This module is the single source of truth for the v1 contract as enforced by
// the JavaScript verifiers:
//   - RFC 8785 JSON Canonicalization Scheme (JCS) canonicalization;
//   - SHA-256 over UTF-8 bytes via node:crypto;
//   - canonical UUID / SHA-256 / visibility / nonnegative-int / finite-float /
//     nullable-string validators;
//   - profile all-or-none coherence;
//   - response coherence (item count, byte count, working-set-v1 render);
//   - startup subject/request invariants;
//   - full manifest reconstruction from frozen inputs;
//   - independent expected-manifest validation before comparison.
//
// It NEVER invokes the Python implementation. `verify.mjs` (positive vectors)
// and `verify_negatives.mjs` (shared negative fixtures) both import from here so
// the two runners cannot drift on the contract while remaining independently
// executable.

import { createHash } from "node:crypto";

// ─── Stable contract constants (must match the Python model exactly) ───
export const SCHEMA = "engram.context-manifest";
export const SCHEMA_VERSION = "1.0";
export const CANONICALIZATION = "rfc8785";
export const MODE = "startup";
export const MEMORY_CONTEXT_VERSION = "memory-context-v2";
export const MANIFEST_CONTRACT_VERSION = "context-manifest-v1";
export const PACKET_RENDER_VERSION = "working-set-v1";
export const PACKET_MEDIA_TYPE = "text/plain; charset=utf-8";
export const VISIBILITY_VALUES = new Set([
  "private",
  "workspace",
  "tenant",
  "public",
]);

// Canonical lowercase UUID (8-4-4-4-12 hex). Identical to the Python
// CanonicalUuidV1 pattern and the normative JSON Schema pattern.
export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// sha256:<64 lowercase hexadecimal characters>
export const SHA256_RE = /^sha256:[0-9a-f]{64}$/;

// Exact required top-level keys of a context-manifest-v1 object (no unknown
// keys allowed).
export const REQUIRED_TOP_LEVEL_KEYS = [
  "schema",
  "schema_version",
  "canonicalization",
  "mode",
  "subject",
  "request",
  "versions",
  "result",
  "packet",
  "items",
];

// ─── SHA-256 helper ────────────────────────────────────────────────────
export function sha256Hex(s) {
  return "sha256:" + createHash("sha256").update(s, "utf8").digest("hex");
}

// ─── RFC 8785 (JCS) canonicalization ───────────────────────────────────
export function canonicalize(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return canonicalString(value);
  if (typeof value === "number") return canonicalNumber(value);
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalize).join(",") + "]";
  }
  if (typeof value === "object") {
    const keys = Object.keys(value).sort(); // UTF-16 code unit order (JCS)
    const members = keys.map(
      (k) => canonicalString(k) + ":" + canonicalize(value[k])
    );
    return "{" + members.join(",") + "}";
  }
  throw new Error(`JCS: unsupported value type: ${typeof value}`);
}

function canonicalString(s) {
  return JSON.stringify(s);
}

function canonicalNumber(n) {
  if (Number.isNaN(n)) throw new Error("JCS: NaN is not valid JSON");
  if (!Number.isFinite(n)) throw new Error("JCS: Infinity/-Infinity is not valid JSON");
  if (Object.is(n, -0)) return "0";
  return n.toString();
}

// ─── Strict scalar predicates ──────────────────────────────────────────
// JS `typeof` coerces nothing at runtime, but Boolean is a subtype of number
// and must be rejected for every integer/float field (mirrors Python's strict
// model where bool is a subclass of int). NaN/Infinity are rejected for every
// finite-number field.
export function isStrictBool(v) {
  return typeof v === "boolean";
}
export function isStrictInt(v) {
  return (
    typeof v === "number" &&
    Number.isInteger(v) &&
    Number.isFinite(v) &&
    !isStrictBool(v)
  );
}
export function isStrictStr(v) {
  return typeof v === "string";
}
export function isFiniteNumber(v) {
  return typeof v === "number" && Number.isFinite(v) && !isStrictBool(v);
}

// ─── Contract validators ───────────────────────────────────────────────
// Each validator throws a contract-named Error on violation and returns the
// validated value on success. They never normalize a noncanonical input into a
// valid one (e.g. uppercase UUID, compact UUID, or a string with surrounding
// whitespace are all rejected, not trimmed/cased).

export function requireCanonicalUuid(value, where) {
  if (!isStrictStr(value)) {
    throw new Error(
      `${where} must be a canonical UUID string, got ${typeof value}`
    );
  }
  if (!UUID_RE.test(value)) {
    throw new Error(
      `${where} must be a canonical lowercase UUID (8-4-4-4-12), got ${JSON.stringify(value)}`
    );
  }
  return value;
}

export function requireOptionalCanonicalUuid(value, where) {
  if (value === null) return null;
  if (value === undefined) {
    throw new Error(
      `${where} must be a canonical UUID or null; undefined is not allowed`
    );
  }
  return requireCanonicalUuid(value, where);
}

export function requireSha256(value, where) {
  if (!isStrictStr(value)) {
    throw new Error(`${where} must be a sha256 string, got ${typeof value}`);
  }
  if (!SHA256_RE.test(value)) {
    throw new Error(
      `${where} must be sha256:<64 lowercase hex>, got ${JSON.stringify(value)}`
    );
  }
  return value;
}

export function requireOptionalSha256(value, where) {
  if (value === null) return null;
  if (value === undefined) {
    throw new Error(
      `${where} must be a sha256 string or null; undefined is not allowed`
    );
  }
  return requireSha256(value, where);
}

export function requireNonNegativeInt(value, where) {
  // Reject Boolean and non-integer numbers explicitly, then require >= 0.
  if (isStrictBool(value)) {
    throw new Error(
      `${where} must be a nonnegative integer (not bool), got boolean`
    );
  }
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new Error(
      `${where} must be a nonnegative integer, got ${typeof value}`
    );
  }
  if (!Number.isFinite(value)) {
    throw new Error(`${where} must be a nonnegative integer (not NaN/Infinity)`);
  }
  if (value < 0) {
    throw new Error(`${where} must be a nonnegative integer, got ${value}`);
  }
  return value;
}

export function requireOptionalNonNegativeInt(value, where) {
  if (value === null) return null;
  if (value === undefined) {
    throw new Error(
      `${where} must be a nonnegative integer or null; undefined is not allowed`
    );
  }
  return requireNonNegativeInt(value, where);
}

export function requireBool(value, where) {
  if (!isStrictBool(value)) {
    throw new Error(`${where} must be a boolean, got ${typeof value}`);
  }
  return value;
}

export function requireFiniteFloat(value, where) {
  // Reject Boolean first, then require a finite number.
  if (isStrictBool(value)) {
    throw new Error(
      `${where} must be a finite number (not bool), got boolean`
    );
  }
  if (!isFiniteNumber(value)) {
    throw new Error(
      `${where} must be a finite number (not NaN/Infinity), got ${typeof value}`
    );
  }
  return value;
}

export function requireOptionalFiniteFloat(value, where) {
  if (value === null) return null;
  if (value === undefined) {
    throw new Error(
      `${where} must be a finite number or null; undefined is not allowed`
    );
  }
  return requireFiniteFloat(value, where);
}

export function requireStr(value, where) {
  if (!isStrictStr(value)) {
    throw new Error(`${where} must be a string, got ${typeof value}`);
  }
  return value;
}

export function requireOptionalString(value, where) {
  // Nullable string field. Undefined is NOT silently coerced to null: the
  // frozen vector format requires explicit null for nullable contract fields.
  if (value === null) return null;
  if (value === undefined) {
    throw new Error(`${where} must be a string or null; undefined is not allowed`);
  }
  return requireStr(value, where);
}

export function requireVisibility(value, where) {
  requireStr(value, where);
  if (!VISIBILITY_VALUES.has(value)) {
    throw new Error(
      `${where} must be one of ${[...VISIBILITY_VALUES].join(", ")}, got ${JSON.stringify(value)}`
    );
  }
  return value;
}

export function requireStrList(value, where) {
  if (!Array.isArray(value)) {
    throw new Error(`${where} must be a list of strings, got ${typeof value}`);
  }
  for (let i = 0; i < value.length; i++) {
    if (!isStrictStr(value[i]) || typeof value[i] === "boolean") {
      throw new Error(`${where}[${i}] must be a string, got ${typeof value[i]}`);
    }
  }
  return value;
}

// ─── Profile coherence (mirrors Python model_validator + JSON Schema oneOf) ─
// memory_profile_id / memory_profile_revision_id / memory_profile_version must be
// ALL null OR ALL non-null-and-valid together. Every partial combination is
// rejected. The three fields are NOT inferred from one another.
export function assertProfileCoherence(profileId, revisionId, version, where) {
  const idSet = profileId !== null;
  const revSet = revisionId !== null;
  const verSet = version !== null;
  const set = [idSet, revSet, verSet];
  if (set.some((s) => s) && !set.every((s) => s)) {
    throw new Error(
      `${where}: profile fields must be all null or all non-null together ` +
        `(memory_profile_id=${JSON.stringify(profileId)}, ` +
        `memory_profile_revision_id=${JSON.stringify(revisionId)}, ` +
        `memory_profile_version=${JSON.stringify(version)})`
    );
  }
  if (idSet) {
    // When profiled, validate the canonical forms.
    requireCanonicalUuid(profileId, `${where}.memory_profile_id`);
    requireCanonicalUuid(revisionId, `${where}.memory_profile_revision_id`);
    requireNonNegativeInt(version, `${where}.memory_profile_version`);
  }
}

// ─── Response coherence (reject incoherent input before reconstruction) ─
export function reconstructWorkingSetV1(items) {
  return items.map((i) => `[${i.kind}] ${i.content}`).join("\n");
}

export function assertResponseCoherence(name, response) {
  if (!Array.isArray(response.items)) {
    throw new Error(`${name}: response.items must be an array`);
  }
  const itemCount = requireNonNegativeInt(
    response.item_count,
    `${name}: response.item_count`
  );
  if (itemCount !== response.items.length) {
    throw new Error(
      `${name}: response.item_count (${itemCount}) != len(items) (${response.items.length})`
    );
  }
  const byteCount = requireNonNegativeInt(
    response.byte_count,
    `${name}: response.byte_count`
  );
  let derived = 0;
  for (const it of response.items) {
    requireStr(it.content, `${name}: response item content`);
    derived += Buffer.byteLength(it.content, "utf8");
  }
  if (byteCount !== derived) {
    throw new Error(
      `${name}: response.byte_count (${byteCount}) != derived (${derived})`
    );
  }
  requireNonNegativeInt(
    response.pinned_omitted_count,
    `${name}: response.pinned_omitted_count`
  );
  requireNonNegativeInt(response.omitted_count, `${name}: response.omitted_count`);
  requireOptionalString(response.message, `${name}: response.message`);

  requireStr(response.working_set, `${name}: response.working_set`);
  const reconstructed = reconstructWorkingSetV1(response.items);
  if (response.working_set !== reconstructed) {
    throw new Error(
      `${name}: response.working_set does not match the working-set-v1 render of items`
    );
  }
}

// ─── Subject / request / versions validation ───────────────────────────

export function validateSubject(name, subject) {
  requireCanonicalUuid(subject.tenant_id, `${name}: subject.tenant_id`);
  requireCanonicalUuid(subject.principal_id, `${name}: subject.principal_id`);
  requireOptionalCanonicalUuid(subject.workspace_id, `${name}: subject.workspace_id`);
  if (subject.memory_context_version !== MEMORY_CONTEXT_VERSION) {
    throw new Error(`${name}: subject.memory_context_version mismatch`);
  }
  const profileId = subject.memory_profile_id ?? null;
  const revisionId = subject.memory_profile_revision_id ?? null;
  const version = subject.memory_profile_version ?? null;
  if (subject.memory_profile_id === undefined) {
    throw new Error(`${name}: subject.memory_profile_id must not be undefined`);
  }
  if (subject.memory_profile_revision_id === undefined) {
    throw new Error(`${name}: subject.memory_profile_revision_id must not be undefined`);
  }
  if (subject.memory_profile_version === undefined) {
    throw new Error(`${name}: subject.memory_profile_version must not be undefined`);
  }
  if (profileId !== null) {
    requireCanonicalUuid(profileId, `${name}: subject.memory_profile_id`);
  }
  if (revisionId !== null) {
    requireCanonicalUuid(revisionId, `${name}: subject.memory_profile_revision_id`);
  }
  if (version !== null) {
    requireNonNegativeInt(version, `${name}: subject.memory_profile_version`);
  }
  assertProfileCoherence(profileId, revisionId, version, `${name}: subject`);
}

export function validateRequestedDescriptor(name, requested) {
  requireBool(requested.workspace_supplied, `${name}: requested.workspace_supplied`);
  requireOptionalNonNegativeInt(requested.byte_budget, `${name}: requested.byte_budget`);
  requireOptionalNonNegativeInt(requested.token_budget, `${name}: requested.token_budget`);
  requireOptionalNonNegativeInt(requested.item_budget, `${name}: requested.item_budget`);
}

export function validateEffectiveDescriptor(name, effective) {
  requireOptionalCanonicalUuid(effective.workspace_id, `${name}: effective.workspace_id`);
  requireOptionalNonNegativeInt(effective.byte_budget, `${name}: effective.byte_budget`);
  requireOptionalNonNegativeInt(effective.token_budget, `${name}: effective.token_budget`);
  requireOptionalNonNegativeInt(effective.item_budget, `${name}: effective.item_budget`);
}

export function validateStartupRequest(name, request, subject) {
  // Startup query_digest must be null.
  if (request.query_digest !== null) {
    throw new Error(`${name}: startup request.query_digest must be null`);
  }
  // subject.workspace_id must equal request.effective.workspace_id.
  if (subject.workspace_id !== request.effective.workspace_id) {
    throw new Error(
      `${name}: subject.workspace_id must equal request.effective.workspace_id`
    );
  }
  // Startup v1 effective.item_budget must be null.
  if (request.effective.item_budget !== null) {
    throw new Error(
      `${name}: startup v1 effective.item_budget must be null (startup does not enforce an item budget)`
    );
  }
  validateRequestedDescriptor(name, request.requested);
  validateEffectiveDescriptor(name, request.effective);
}

export function validateVersions(name, versions) {
  requireStr(versions.scoring_version, `${name}: versions.scoring_version`);
  requireStr(versions.config_version, `${name}: versions.config_version`);
  requireStr(
    versions.candidate_strategy_version,
    `${name}: versions.candidate_strategy_version`
  );
  if (versions.manifest_contract_version !== MANIFEST_CONTRACT_VERSION) {
    throw new Error(`${name}: versions.manifest_contract_version mismatch`);
  }
  if (versions.packet_render_version !== PACKET_RENDER_VERSION) {
    throw new Error(`${name}: versions.packet_render_version mismatch`);
  }
}

// ─── Full manifest reconstruction from input ───────────────────────────
export function buildManifestFromInput(name, inp) {
  const subject = inp.subject_context;
  const request = inp.request_context;
  const versions = inp.decision_versions;
  const response = inp.response;

  // Validate the complete input contract BEFORE reconstruction.
  validateSubject(name, subject);
  validateVersions(name, versions);
  validateStartupRequest(name, request, subject);

  // Coherence first (reject incoherent input).
  assertResponseCoherence(name, response);

  // Packet hash over exact working_set bytes.
  const packetBytes = response.working_set;
  const packetHash = sha256Hex(packetBytes);

  // Ordered item snapshots.
  let servedContentByteCount = 0;
  const items = [];
  for (let ordinal = 0; ordinal < response.items.length; ordinal++) {
    const raw = response.items[ordinal];
    const content = requireStr(raw.content, `${name}: item content`);
    servedContentByteCount += Buffer.byteLength(content, "utf8");
    items.push({
      ordinal,
      item_id: requireCanonicalUuid(raw.id, `${name}: item id`),
      kind: requireStr(raw.kind, `${name}: item kind`),
      served_content_hash: sha256Hex(content),
      review_status: requireStr(raw.review_status, `${name}: item review_status`),
      authority: requireNonNegativeInt(raw.authority, `${name}: item authority`),
      visibility: requireVisibility(raw.visibility, `${name}: item visibility`),
      workspace_id:
        raw.workspace_id === null
          ? null
          : requireCanonicalUuid(raw.workspace_id, `${name}: item workspace_id`),
      score:
        raw.score === null
          ? null
          : requireFiniteFloat(raw.score, `${name}: item score`),
      reasons: requireStrList(raw.reasons, `${name}: item reasons`),
      warnings: requireStrList(raw.warnings, `${name}: item warnings`),
      pinned: requireBool(raw.pinned, `${name}: item pinned`),
      importance: requireFiniteFloat(raw.importance, `${name}: item importance`),
      source_trust: requireFiniteFloat(raw.source_trust, `${name}: item source_trust`),
      memory_confidence: requireFiniteFloat(
        raw.memory_confidence,
        `${name}: item memory_confidence`
      ),
      human_verified: requireBool(raw.human_verified, `${name}: item human_verified`),
      conflict_type:
        raw.conflict_type === null
          ? null
          : requireStr(raw.conflict_type, `${name}: item conflict_type`),
      conflict_resolution_status:
        raw.conflict_resolution_status === null
          ? null
          : requireStr(
              raw.conflict_resolution_status,
              `${name}: item conflict_resolution_status`
            ),
    });
  }

  // request_digest: SHA-256 of canonical request descriptor (without digest).
  const requestDescriptor = {
    requested: request.requested,
    effective: request.effective,
    query_digest: request.query_digest,
  };
  const requestDigest = sha256Hex(canonicalize(requestDescriptor));

  const manifest = {
    schema: SCHEMA,
    schema_version: SCHEMA_VERSION,
    canonicalization: CANONICALIZATION,
    mode: MODE,
    subject: {
      tenant_id: subject.tenant_id,
      principal_id: subject.principal_id,
      workspace_id: subject.workspace_id,
      memory_context_version: subject.memory_context_version,
      memory_profile_id: subject.memory_profile_id,
      memory_profile_revision_id: subject.memory_profile_revision_id,
      memory_profile_version: subject.memory_profile_version,
    },
    request: {
      requested: request.requested,
      effective: request.effective,
      query_digest: request.query_digest,
      request_digest: requestDigest,
    },
    versions: {
      scoring_version: versions.scoring_version,
      config_version: versions.config_version,
      candidate_strategy_version: versions.candidate_strategy_version,
      manifest_contract_version: versions.manifest_contract_version,
      packet_render_version: versions.packet_render_version,
    },
    result: {
      item_count: response.items.length,
      served_content_byte_count: servedContentByteCount,
      rendered_packet_byte_count: Buffer.byteLength(response.working_set, "utf8"),
      pinned_omitted_count: response.pinned_omitted_count,
      omitted_count: response.omitted_count,
      message: response.message === undefined ? null : response.message,
    },
    packet: {
      media_type: PACKET_MEDIA_TYPE,
      render_version: PACKET_RENDER_VERSION,
      hash: packetHash,
    },
    items,
  };
  return manifest;
}

// ─── Independent expected-manifest validation ──────────────────────────
// The verifier must independently validate the frozen expected manifest before
// comparing it with reconstruction. This proves the expected wire object is
// itself contract-conformant, not merely byte-equal to a reconstruction.
export function validateExpectedManifest(name, manifest) {
  if (manifest === null || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new Error(`${name}: expected.manifest must be an object`);
  }
  const keys = Object.keys(manifest);
  for (const k of REQUIRED_TOP_LEVEL_KEYS) {
    if (!(k in manifest)) {
      throw new Error(
        `${name}: expected.manifest missing required top-level key '${k}'`
      );
    }
  }
  for (const k of keys) {
    if (!REQUIRED_TOP_LEVEL_KEYS.includes(k)) {
      throw new Error(
        `${name}: expected.manifest has unknown top-level key '${k}'`
      );
    }
  }
  if (manifest.schema !== SCHEMA) {
    throw new Error(`${name}: expected.manifest.schema must be ${SCHEMA}`);
  }
  if (manifest.schema_version !== SCHEMA_VERSION) {
    throw new Error(
      `${name}: expected.manifest.schema_version must be ${SCHEMA_VERSION}`
    );
  }
  if (manifest.canonicalization !== CANONICALIZATION) {
    throw new Error(
      `${name}: expected.manifest.canonicalization must be ${CANONICALIZATION}`
    );
  }
  if (manifest.mode !== MODE) {
    throw new Error(`${name}: expected.manifest.mode must be ${MODE}`);
  }

  const subject = manifest.subject;
  requireCanonicalUuid(subject.tenant_id, `${name}: manifest.subject.tenant_id`);
  requireCanonicalUuid(subject.principal_id, `${name}: manifest.subject.principal_id`);
  requireOptionalCanonicalUuid(
    subject.workspace_id,
    `${name}: manifest.subject.workspace_id`
  );
  if (subject.memory_context_version !== MEMORY_CONTEXT_VERSION) {
    throw new Error(`${name}: manifest.subject.memory_context_version mismatch`);
  }
  assertProfileCoherence(
    subject.memory_profile_id ?? null,
    subject.memory_profile_revision_id ?? null,
    subject.memory_profile_version ?? null,
    `${name}: manifest.subject`
  );

  const request = manifest.request;
  validateRequestedDescriptor(name, request.requested);
  validateEffectiveDescriptor(name, request.effective);
  requireOptionalSha256(request.query_digest, `${name}: manifest.request.query_digest`);
  if (request.query_digest !== null) {
    throw new Error(`${name}: manifest.request.query_digest must be null for startup`);
  }
  requireSha256(request.request_digest, `${name}: manifest.request.request_digest`);
  if (request.effective.item_budget !== null) {
    throw new Error(
      `${name}: manifest.request.effective.item_budget must be null for startup`
    );
  }

  validateVersions(name, manifest.versions);

  const result = manifest.result;
  requireNonNegativeInt(result.item_count, `${name}: manifest.result.item_count`);
  requireNonNegativeInt(
    result.served_content_byte_count,
    `${name}: manifest.result.served_content_byte_count`
  );
  requireNonNegativeInt(
    result.rendered_packet_byte_count,
    `${name}: manifest.result.rendered_packet_byte_count`
  );
  requireNonNegativeInt(
    result.pinned_omitted_count,
    `${name}: manifest.result.pinned_omitted_count`
  );
  requireNonNegativeInt(result.omitted_count, `${name}: manifest.result.omitted_count`);
  requireOptionalString(result.message, `${name}: manifest.result.message`);

  const packet = manifest.packet;
  if (packet.media_type !== PACKET_MEDIA_TYPE) {
    throw new Error(`${name}: manifest.packet.media_type must be ${PACKET_MEDIA_TYPE}`);
  }
  if (packet.render_version !== PACKET_RENDER_VERSION) {
    throw new Error(
      `${name}: manifest.packet.render_version must be ${PACKET_RENDER_VERSION}`
    );
  }
  requireSha256(packet.hash, `${name}: manifest.packet.hash`);

  if (!Array.isArray(manifest.items)) {
    throw new Error(`${name}: manifest.items must be an array`);
  }
  if (result.item_count !== manifest.items.length) {
    throw new Error(
      `${name}: manifest.result.item_count (${result.item_count}) != items.length (${manifest.items.length})`
    );
  }
  for (let i = 0; i < manifest.items.length; i++) {
    const it = manifest.items[i];
    requireNonNegativeInt(it.ordinal, `${name}: manifest.items[${i}].ordinal`);
    if (it.ordinal !== i) {
      throw new Error(
        `${name}: manifest.items[${i}].ordinal (${it.ordinal}) must equal array position (${i})`
      );
    }
    requireCanonicalUuid(it.item_id, `${name}: manifest.items[${i}].item_id`);
    requireStr(it.kind, `${name}: manifest.items[${i}].kind`);
    requireSha256(
      it.served_content_hash,
      `${name}: manifest.items[${i}].served_content_hash`
    );
    requireStr(it.review_status, `${name}: manifest.items[${i}].review_status`);
    requireNonNegativeInt(it.authority, `${name}: manifest.items[${i}].authority`);
    requireVisibility(it.visibility, `${name}: manifest.items[${i}].visibility`);
    requireOptionalCanonicalUuid(
      it.workspace_id,
      `${name}: manifest.items[${i}].workspace_id`
    );
    requireOptionalFiniteFloat(it.score, `${name}: manifest.items[${i}].score`);
    requireStrList(it.reasons, `${name}: manifest.items[${i}].reasons`);
    requireStrList(it.warnings, `${name}: manifest.items[${i}].warnings`);
    requireBool(it.pinned, `${name}: manifest.items[${i}].pinned`);
    requireFiniteFloat(it.importance, `${name}: manifest.items[${i}].importance`);
    requireFiniteFloat(it.source_trust, `${name}: manifest.items[${i}].source_trust`);
    requireFiniteFloat(
      it.memory_confidence,
      `${name}: manifest.items[${i}].memory_confidence`
    );
    requireBool(it.human_verified, `${name}: manifest.items[${i}].human_verified`);
    requireOptionalString(it.conflict_type, `${name}: manifest.items[${i}].conflict_type`);
    requireOptionalString(
      it.conflict_resolution_status,
      `${name}: manifest.items[${i}].conflict_resolution_status`
    );
  }
}

// ─── Post-reconstruction semantic invariants ───────────────────────────
// After the manifest is built, prove the wire-level invariants that depend on
// the full object, not just a single field:
//   - result.item_count == items.length;
//   - ordinals are exactly 0..n-1;
//   - packet.hash matches an independent hash of the reconstructed working set;
//   - request_digest matches an independent derivation over the descriptor;
//   - all served_content_hashes match exact content bytes;
//   - profile coherence holds (already enforced, restated for completeness);
//   - all UUIDs/hashes are canonical and counts/budgets nonnegative.
export function assertPostReconstructionInvariants(name, manifest, response) {
  if (manifest.result.item_count !== manifest.items.length) {
    throw new Error(
      `${name}: post-reconstruction item_count (${manifest.result.item_count}) != items.length (${manifest.items.length})`
    );
  }
  for (let i = 0; i < manifest.items.length; i++) {
    if (manifest.items[i].ordinal !== i) {
      throw new Error(
        `${name}: post-reconstruction ordinal at ${i} is ${manifest.items[i].ordinal}`
      );
    }
  }
  const ws = reconstructWorkingSetV1(response.items);
  if (manifest.packet.hash !== sha256Hex(ws)) {
    throw new Error(`${name}: post-reconstruction packet.hash mismatch`);
  }
  const descriptor = {
    requested: manifest.request.requested,
    effective: manifest.request.effective,
    query_digest: manifest.request.query_digest,
  };
  if (manifest.request.request_digest !== sha256Hex(canonicalize(descriptor))) {
    throw new Error(`${name}: post-reconstruction request_digest mismatch`);
  }
  if (manifest.items.length !== response.items.length) {
    throw new Error(`${name}: post-reconstruction items length mismatch`);
  }
  for (let i = 0; i < manifest.items.length; i++) {
    if (manifest.items[i].served_content_hash !== sha256Hex(response.items[i].content)) {
      throw new Error(
        `${name}: post-reconstruction served_content_hash[${i}] mismatch`
      );
    }
  }
}
