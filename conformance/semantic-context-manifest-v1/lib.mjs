// Independent JavaScript reconstruction for semantic-context-manifest-v1.
// This module uses Node only. It never invokes Python or hashes a prebuilt
// expected manifest.

import {
  canonicalize,
  requireBool,
  requireCanonicalUuid,
  requireFiniteFloat,
  requireInt,
  requireNonNegativeInt,
  requireOptionalCanonicalUuid,
  requireOptionalFiniteFloat,
  requireOptionalNonNegativeInt,
  requireOptionalString,
  requireSha256,
  requireStr,
  requireStrList,
  requireVisibility,
  sha256Hex,
} from "../context-manifest-v1/lib.mjs";

export { canonicalize } from "../context-manifest-v1/lib.mjs";

export const SCHEMA = "engram.semantic-context-manifest";
export const SCHEMA_VERSION = "1.0";
export const CONTRACT_VERSION = "semantic-context-manifest-v1";
const QUERY_DOMAIN = "engram.semantic-context-manifest-v1/query\0";

const ADMISSION_TIERS = ["none", "semantic_exploratory", "semantic_governed", "startup"];
const RISK_STATES = ["low", "medium", "high", "unknown", "not_applicable"];
const RETENTION_STATES = ["retain", "transient", "noise", "uncertain", "unknown"];
const ASSERTION_MODES = [
  "direct_statement", "tool_observation", "quoted_source", "derived_summary", "inference",
  "unknown",
];
const ORIGINS = ["user", "assistant", "system", "tool", "unknown"];
const NEXT_ACTIONS = [
  "wait_until", "classification_required", "human_review_required",
  "conflict_resolution_required", "new_evidence_required",
  "policy_reconciliation_required", "none",
];
const ADMISSION_OUTCOMES = [
  "admitted", "would_admit", "cooling", "review_required", "blocked",
  "insufficient_evidence", "unknown", "stale", "not_applicable",
];
const WARNING_CODES = [
  "unreviewed", "evidence_unknown", "evidence_contested", "evidence_insufficient",
  "conflict_unresolved", "disputed", "risk_high", "risk_unknown",
  "admission_assessment_stale", "admission_legacy_import",
];
const REVIEW_STATUSES = ["proposed", "active", "disputed", "rejected", "archived"];
const CONFLICT_TYPES = ["contradiction", "stale", "duplicate", "scope_overlap"];
const CONFLICT_RESOLUTION_STATUSES = ["unresolved", "accepted", "rejected", "merged"];
const PACKING_REASONS = ["ranked", "conflict_pair_preserved", "diversity_fill"];
const RELATIONSHIP_ORIGINS = ["semantic", "graph", "tunnel"];

function exact(value, keys, where) {
  if (value === null || Array.isArray(value) || typeof value !== "object") {
    throw new Error(`${where} must be an object`);
  }
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`${where} has missing or extra fields`);
  }
  return value;
}

function optional(value, validator, where) {
  return value === null ? null : validator(value, where);
}

function enumValue(value, allowed, where) {
  if (!allowed.includes(value)) throw new Error(`${where} is outside the frozen vocabulary`);
  return value;
}

function optionalEnum(value, allowed, where) {
  return value === null ? null : enumValue(value, allowed, where);
}

function enumList(value, allowed, where) {
  requireStrList(value, where);
  value.forEach((entry) => enumValue(entry, allowed, where));
  return value;
}

function profile(subject, where) {
  const values = [
    subject.memory_profile_id,
    subject.memory_profile_revision_id,
    subject.memory_profile_version,
  ];
  if (values.some((value) => value !== null) && values.some((value) => value === null)) {
    throw new Error(`${where} profile identity is partial`);
  }
  requireOptionalCanonicalUuid(subject.memory_profile_id, `${where}.memory_profile_id`);
  requireOptionalCanonicalUuid(
    subject.memory_profile_revision_id,
    `${where}.memory_profile_revision_id`
  );
  requireOptionalNonNegativeInt(
    subject.memory_profile_version,
    `${where}.memory_profile_version`
  );
}

const REF_KEYS = [
  "assessment_id",
  "contract_hash",
  "canonical_hash",
  "purpose",
  "assertion_mode",
  "origin",
];

function assessmentRef(ref, where) {
  exact(ref, REF_KEYS, where);
  requireCanonicalUuid(ref.assessment_id, `${where}.assessment_id`);
  requireSha256(ref.contract_hash, `${where}.contract_hash`);
  requireSha256(ref.canonical_hash, `${where}.canonical_hash`);
  if (ref.purpose !== "combined") throw new Error(`${where}.purpose mismatch`);
  enumValue(ref.assertion_mode, ASSERTION_MODES, `${where}.assertion_mode`);
  enumValue(ref.origin, ORIGINS, `${where}.origin`);
  return ref;
}

function refs(value, where) {
  if (!Array.isArray(value)) throw new Error(`${where} must be an array`);
  value.forEach((ref, index) => assessmentRef(ref, `${where}[${index}]`));
  return value;
}

const FRESH_KEYS = [
  "schema_version",
  "policy_version",
  "policy_artifact_digest",
  "decision_hash",
  "surface_decision",
  "highest_admission_tier",
  "risk_state",
  "epistemic_state",
  "retention_state",
  "effective_assessment_refs",
  "observation_window_hours",
  "eligible_at",
  "next_evaluation_at",
  "blocker_codes",
  "reason_codes",
  "next_actions",
];

function fresh(value, where) {
  exact(value, FRESH_KEYS, where);
  if (value.schema_version !== "engram.admission-assessment.v2") {
    throw new Error(`${where}.schema_version mismatch`);
  }
  requireStr(value.policy_version, `${where}.policy_version`);
  requireSha256(value.policy_artifact_digest, `${where}.policy_artifact_digest`);
  requireSha256(value.decision_hash, `${where}.decision_hash`);
  if (value.surface_decision !== "allow") throw new Error(`${where}.surface_decision`);
  optionalEnum(value.highest_admission_tier, ADMISSION_TIERS, `${where}.highest_admission_tier`);
  optionalEnum(value.risk_state, RISK_STATES, `${where}.risk_state`);
  if (!["supported", "contested", "insufficient_evidence", "unknown"].includes(value.epistemic_state)) {
    throw new Error(`${where}.epistemic_state mismatch`);
  }
  optionalEnum(value.retention_state, RETENTION_STATES, `${where}.retention_state`);
  refs(value.effective_assessment_refs, `${where}.effective_assessment_refs`);
  requireOptionalNonNegativeInt(value.observation_window_hours, `${where}.observation_window_hours`);
  requireOptionalString(value.eligible_at, `${where}.eligible_at`);
  requireOptionalString(value.next_evaluation_at, `${where}.next_evaluation_at`);
  requireStrList(value.blocker_codes, `${where}.blocker_codes`);
  requireStrList(value.reason_codes, `${where}.reason_codes`);
  enumList(value.next_actions, NEXT_ACTIONS, `${where}.next_actions`);
  return value;
}

const PERSISTED_KEYS = [
  "assessment_id",
  "schema_version",
  "policy_contract_version",
  "policy_artifact_digest",
  "decision_hash",
];

function persisted(value, where) {
  exact(value, PERSISTED_KEYS, where);
  requireCanonicalUuid(value.assessment_id, `${where}.assessment_id`);
  if (value.schema_version !== "engram.admission-assessment.v2") {
    throw new Error(`${where}.schema_version mismatch`);
  }
  requireStr(value.policy_contract_version, `${where}.policy_contract_version`);
  requireSha256(value.policy_artifact_digest, `${where}.policy_artifact_digest`);
  requireSha256(value.decision_hash, `${where}.decision_hash`);
  return value;
}

const V2_KEYS = [
  "profile_key",
  "resolution_status",
  "surface",
  "surface_decision",
  "persisted",
  "fresh",
];

function v2(value, where) {
  exact(value, V2_KEYS, where);
  if (value.profile_key !== "risk_aware_shadow_v1") throw new Error(`${where}.profile_key`);
  if (value.resolution_status !== "current") throw new Error(`${where}.resolution_status`);
  if (!["semantic_governed", "semantic_exploratory"].includes(value.surface)) {
    throw new Error(`${where}.surface`);
  }
  if (value.surface_decision !== "allow") throw new Error(`${where}.surface_decision`);
  persisted(value.persisted, `${where}.persisted`);
  fresh(value.fresh, `${where}.fresh`);
  if (
    value.persisted.schema_version !== value.fresh.schema_version ||
    value.persisted.policy_contract_version !== value.fresh.policy_version ||
    value.persisted.policy_artifact_digest !== value.fresh.policy_artifact_digest ||
    value.persisted.decision_hash !== value.fresh.decision_hash
  ) {
    throw new Error(`${where} current persisted/fresh identity mismatch`);
  }
  return value;
}

const ADMISSION_KEYS = [
  "profile",
  "decision",
  "policy_version",
  "reason_codes",
  "assessment_id",
  "assessment_status",
  "assessment_outcome",
  "surface",
  "surface_decision",
  "v2",
];

function admission(value, where) {
  exact(value, ADMISSION_KEYS, where);
  if (!["governed", "exploratory"].includes(value.profile)) throw new Error(`${where}.profile`);
  if (value.decision !== "admit") throw new Error(`${where}.decision`);
  if (value.policy_version !== "recall-admission-v2") throw new Error(`${where}.policy_version`);
  requireStrList(value.reason_codes, `${where}.reason_codes`);
  requireOptionalCanonicalUuid(value.assessment_id, `${where}.assessment_id`);
  if (value.assessment_status !== null && !["current", "stale", "legacy_import"].includes(value.assessment_status)) {
    throw new Error(`${where}.assessment_status`);
  }
  optionalEnum(value.assessment_outcome, ADMISSION_OUTCOMES, `${where}.assessment_outcome`);
  v2(value.v2, `${where}.v2`);
  if (value.surface !== value.v2.surface || value.surface_decision !== value.v2.surface_decision) {
    throw new Error(`${where} V2 identity mismatch`);
  }
  return value;
}

const EVIDENCE_KEYS = [
  "source",
  "profile_key",
  "policy_version",
  "policy_artifact_digest",
  "decision_hash",
  "v2_resolution_status",
  "epistemic_state",
  "risk_state",
  "retention_state",
  "effective_assessment_refs",
];

function evidence(value, where) {
  exact(value, EVIDENCE_KEYS, where);
  if (value.source !== "v2_fresh_evaluation") throw new Error(`${where}.source`);
  if (value.profile_key !== "risk_aware_shadow_v1") throw new Error(`${where}.profile_key`);
  requireStr(value.policy_version, `${where}.policy_version`);
  requireSha256(value.policy_artifact_digest, `${where}.policy_artifact_digest`);
  requireSha256(value.decision_hash, `${where}.decision_hash`);
  if (value.v2_resolution_status !== "current") throw new Error(`${where}.v2_resolution_status`);
  if (!["supported", "contested", "insufficient_evidence", "unknown"].includes(value.epistemic_state)) {
    throw new Error(`${where}.epistemic_state`);
  }
  optionalEnum(value.risk_state, RISK_STATES, `${where}.risk_state`);
  optionalEnum(value.retention_state, RETENTION_STATES, `${where}.retention_state`);
  refs(value.effective_assessment_refs, `${where}.effective_assessment_refs`);
  return value;
}

const RELATIONSHIP_KEYS = [
  "version",
  "origins",
  "direct",
  "direct_semantic_score",
  "source_seed_score",
  "graph_contribution",
  "graph_edge_types",
  "tunnel_labels",
  "relevance_score",
  "components",
];

function relationship(value, where) {
  exact(value, RELATIONSHIP_KEYS, where);
  if (value.version !== "relationship-relevance-v1") throw new Error(`${where}.version`);
  requireStrList(value.origins, `${where}.origins`);
  enumList(value.origins, RELATIONSHIP_ORIGINS, `${where}.origins`);
  requireBool(value.direct, `${where}.direct`);
  requireOptionalFiniteFloat(value.direct_semantic_score, `${where}.direct_semantic_score`);
  requireFiniteFloat(value.source_seed_score, `${where}.source_seed_score`);
  requireFiniteFloat(value.graph_contribution, `${where}.graph_contribution`);
  requireStrList(value.graph_edge_types, `${where}.graph_edge_types`);
  requireStrList(value.tunnel_labels, `${where}.tunnel_labels`);
  requireFiniteFloat(value.relevance_score, `${where}.relevance_score`);
  exact(value.components, ["semantic", "graph", "tunnel"], `${where}.components`);
  for (const key of ["semantic", "graph", "tunnel"]) {
    requireFiniteFloat(value.components[key], `${where}.components.${key}`);
  }
  return value;
}

function expansion(value, where) {
  const keys = [
    "version", "seed_count", "discovered_neighbors", "graph_neighbors",
    "tunnel_neighbors", "admitted_expanded", "withheld_expanded",
  ];
  exact(value, keys, where);
  if (value.version !== "relationship-relevance-v1") throw new Error(`${where}.version`);
  keys.slice(1).forEach((key) => requireNonNegativeInt(value[key], `${where}.${key}`));
  return value;
}

function packing(value, where) {
  exact(value, ["version", "selected_count", "conflict_pairs_preserved", "omitted"], where);
  if (value.version !== "recall-packing-v1") throw new Error(`${where}.version`);
  requireNonNegativeInt(value.selected_count, `${where}.selected_count`);
  requireNonNegativeInt(value.conflict_pairs_preserved, `${where}.conflict_pairs_preserved`);
  const allowed = new Set(["redundant_known_root", "conflict_counterpart_budget", "budget"]);
  for (const [key, count] of Object.entries(value.omitted)) {
    if (!allowed.has(key)) throw new Error(`${where}.omitted extra field`);
    requireNonNegativeInt(count, `${where}.omitted.${key}`);
  }
  return value;
}

function item(raw, ordinal, where) {
  const admissionValue = optional(raw.admission, admission, `${where}.admission`);
  const evidenceValue = optional(raw.evidence, evidence, `${where}.evidence`);
  const relationshipValue = optional(raw.relationship, relationship, `${where}.relationship`);
  const v2Fields = [admissionValue, evidenceValue, raw.relevance_score, raw.utility_score];
  if (v2Fields.some((value) => value !== null) && v2Fields.some((value) => value === null)) {
    throw new Error(`${where} V2 fields must be all set or all null`);
  }
  if (admissionValue !== null) {
    const freshValue = admissionValue.v2.fresh;
    for (const key of [
      "profile_key", "policy_version", "policy_artifact_digest", "decision_hash",
      "epistemic_state", "risk_state", "retention_state", "effective_assessment_refs",
    ]) {
      const expected = key === "profile_key" ? admissionValue.v2.profile_key : freshValue[key];
      if (canonicalize(evidenceValue[key]) !== canonicalize(expected)) {
        throw new Error(`${where} evidence identity mismatch`);
      }
    }
  }
  if (relationshipValue !== null && relationshipValue.relevance_score !== raw.relevance_score) {
    throw new Error(`${where} relationship relevance mismatch`);
  }
  optionalEnum(raw.packing_reason, PACKING_REASONS, `${where}.packing_reason`);
  if (admissionValue !== null && raw.packing_reason === null) {
    throw new Error(`${where} V2 item requires a packing reason`);
  }
  return {
    ordinal,
    item_id: requireCanonicalUuid(raw.id, `${where}.id`),
    kind: requireStr(raw.kind, `${where}.kind`),
    served_content_hash: sha256Hex(requireStr(raw.content, `${where}.content`)),
    review_status: enumValue(raw.review_status, REVIEW_STATUSES, `${where}.review_status`),
    authority: requireInt(raw.authority, `${where}.authority`),
    visibility: requireVisibility(raw.visibility, `${where}.visibility`),
    workspace_id: requireOptionalCanonicalUuid(raw.workspace_id, `${where}.workspace_id`),
    pinned: requireBool(raw.pinned, `${where}.pinned`),
    score: requireOptionalFiniteFloat(raw.score, `${where}.score`),
    trust_score: requireOptionalFiniteFloat(raw.trust_score, `${where}.trust_score`),
    relevance_score: requireOptionalFiniteFloat(raw.relevance_score, `${where}.relevance_score`),
    utility_score: requireOptionalFiniteFloat(raw.utility_score, `${where}.utility_score`),
    reasons: requireStrList(raw.reasons, `${where}.reasons`),
    warnings: requireStrList(raw.warnings, `${where}.warnings`),
    warning_codes: raw.warning_codes === null ? null : enumList(raw.warning_codes, WARNING_CODES, `${where}.warning_codes`),
    conflict_type: optionalEnum(raw.conflict_type, CONFLICT_TYPES, `${where}.conflict_type`),
    conflict_resolution_status: optionalEnum(raw.conflict_resolution_status, CONFLICT_RESOLUTION_STATUSES, `${where}.conflict_resolution_status`),
    admission: admissionValue,
    evidence: evidenceValue,
    relationship: relationshipValue,
    packing_reason: raw.packing_reason,
  };
}

export function buildManifestFromInput(name, input) {
  exact(input, ["query", "context", "packet"], `${name}.input`);
  const ctx = input.context;
  const pkt = input.packet;
  requireStr(input.query, `${name}.query`);
  exact(ctx, [
    "tenant_id", "principal_id", "workspace_id", "memory_context_version",
    "memory_profile_id", "memory_profile_revision_id", "memory_profile_version",
    "workspace_supplied", "requested_byte_budget", "requested_token_budget",
    "requested_item_budget", "effective_byte_budget", "effective_token_budget",
    "effective_item_budget", "recall_profile", "recall_profile_contract_version",
    "scoring_version", "signals_version", "admission_policy",
    "relationship_relevance_version", "packing_version", "config_version",
  ], `${name}.context`);
  exact(pkt, [
    "items", "working_set", "item_count", "byte_count", "candidate_count",
    "omitted_count", "omitted_by_admission", "expansion", "packing", "message",
  ], `${name}.packet`);
  requireCanonicalUuid(ctx.tenant_id, `${name}.tenant_id`);
  requireCanonicalUuid(ctx.principal_id, `${name}.principal_id`);
  requireOptionalCanonicalUuid(ctx.workspace_id, `${name}.workspace_id`);
  if (ctx.memory_context_version !== "memory-context-v2") throw new Error(`${name}.memory_context_version`);
  profile(ctx, `${name}.context`);
  requireBool(ctx.workspace_supplied, `${name}.workspace_supplied`);
  for (const key of ["requested_byte_budget", "requested_token_budget", "requested_item_budget", "effective_byte_budget", "effective_token_budget", "effective_item_budget"]) {
    requireOptionalNonNegativeInt(ctx[key], `${name}.${key}`);
  }
  if (!["legacy", "governed", "exploratory"].includes(ctx.recall_profile)) throw new Error(`${name}.recall_profile`);
  for (const key of ["recall_profile_contract_version", "scoring_version", "config_version"]) requireStr(ctx[key], `${name}.${key}`);
  requireOptionalString(ctx.signals_version, `${name}.signals_version`);
  optionalEnum(ctx.admission_policy, ["recall-admission-v2"], `${name}.admission_policy`);
  optionalEnum(ctx.relationship_relevance_version, ["relationship-relevance-v1"], `${name}.relationship_relevance_version`);
  optionalEnum(ctx.packing_version, ["recall-packing-v1"], `${name}.packing_version`);
  if (!Array.isArray(pkt.items)) throw new Error(`${name}.items`);
  requireNonNegativeInt(pkt.item_count, `${name}.item_count`);
  requireNonNegativeInt(pkt.byte_count, `${name}.byte_count`);
  requireNonNegativeInt(pkt.candidate_count, `${name}.candidate_count`);
  requireNonNegativeInt(pkt.omitted_count, `${name}.omitted_count`);
  if (pkt.item_count !== pkt.items.length) throw new Error(`${name} item count mismatch`);
  const actualBytes = pkt.items.reduce((total, value) => total + Buffer.byteLength(requireStr(value.content, `${name}.content`), "utf8"), 0);
  if (actualBytes !== pkt.byte_count) throw new Error(`${name} byte count mismatch`);
  const rendered = pkt.items.map((value) => `[${value.kind}] ${value.content}`).join("\n");
  if (rendered !== pkt.working_set) throw new Error(`${name} working set mismatch`);
  const tokenCount = pkt.items.reduce((total, value) => total + Math.max(1, Math.floor(Buffer.byteLength(value.content, "utf8") / 4)), 0);
  if (ctx.effective_byte_budget !== null && actualBytes > ctx.effective_byte_budget) throw new Error(`${name} byte budget exceeded`);
  if (ctx.effective_token_budget !== null && tokenCount > ctx.effective_token_budget) throw new Error(`${name} token budget exceeded`);
  if (ctx.effective_item_budget !== null && pkt.item_count > ctx.effective_item_budget) throw new Error(`${name} item budget exceeded`);
  const expansionValue = optional(pkt.expansion, expansion, `${name}.expansion`);
  const packingValue = optional(pkt.packing, packing, `${name}.packing`);
  if (ctx.recall_profile !== "legacy" && (packingValue === null || packingValue.selected_count !== pkt.item_count)) throw new Error(`${name} packing selected count mismatch`);
  const items = pkt.items.map((value, index) => item(value, index, `${name}.items[${index}]`));
  const queryDigest = sha256Hex(QUERY_DOMAIN + input.query);
  const requested = {
    workspace_supplied: ctx.workspace_supplied,
    byte_budget: ctx.requested_byte_budget,
    token_budget: ctx.requested_token_budget,
    item_budget: ctx.requested_item_budget,
  };
  const effective = {
    workspace_id: ctx.workspace_id,
    byte_budget: ctx.effective_byte_budget,
    token_budget: ctx.effective_token_budget,
    item_budget: ctx.effective_item_budget,
  };
  const manifest = {
    schema: SCHEMA,
    schema_version: SCHEMA_VERSION,
    canonicalization: "rfc8785",
    mode: "semantic",
    subject: {
      tenant_id: ctx.tenant_id,
      principal_id: ctx.principal_id,
      workspace_id: ctx.workspace_id,
      memory_context_version: ctx.memory_context_version,
      memory_profile_id: ctx.memory_profile_id,
      memory_profile_revision_id: ctx.memory_profile_revision_id,
      memory_profile_version: ctx.memory_profile_version,
    },
    request: {
      requested,
      effective,
      query_digest: queryDigest,
      request_digest: sha256Hex(canonicalize({ requested, effective, query_digest: queryDigest })),
    },
    versions: {
      recall_profile: ctx.recall_profile,
      recall_profile_contract_version: ctx.recall_profile_contract_version,
      scoring_version: ctx.scoring_version,
      signals_version: ctx.signals_version,
      admission_policy: ctx.admission_policy,
      relationship_relevance_version: ctx.relationship_relevance_version,
      packing_version: ctx.packing_version,
      config_version: ctx.config_version,
      manifest_contract_version: CONTRACT_VERSION,
      packet_render_version: "working-set-v1",
    },
    result: {
      item_count: pkt.item_count,
      served_content_byte_count: pkt.byte_count,
      rendered_packet_byte_count: Buffer.byteLength(pkt.working_set, "utf8"),
      candidate_count: pkt.candidate_count,
      omitted_count: pkt.omitted_count,
      omitted_by_admission: pkt.omitted_by_admission,
      expansion: expansionValue,
      packing: packingValue,
      message: pkt.message,
    },
    packet: {
      media_type: "text/plain; charset=utf-8",
      render_version: "working-set-v1",
      hash: sha256Hex(pkt.working_set),
    },
    items,
  };
  validateProfileCoherence(name, manifest);
  return manifest;
}

function validateProfileCoherence(name, manifest) {
  const profileName = manifest.versions.recall_profile;
  const candidateVersions = [
    manifest.versions.admission_policy,
    manifest.versions.relationship_relevance_version,
    manifest.versions.packing_version,
  ];
  if (profileName === "legacy") {
    if (candidateVersions.some((value) => value !== null)) {
      throw new Error(`${name} legacy profile declares candidate protocol versions`);
    }
  } else if (
    manifest.versions.admission_policy !== "recall-admission-v2" ||
    manifest.versions.packing_version !== "recall-packing-v1"
  ) {
    throw new Error(`${name} candidate profile lacks exact protocol versions`);
  }
  if ((manifest.result.expansion === null) !== (manifest.versions.relationship_relevance_version === null)) {
    throw new Error(`${name} relationship expansion/version mismatch`);
  }
  if (manifest.result.expansion !== null && manifest.result.expansion.version !== manifest.versions.relationship_relevance_version) {
    throw new Error(`${name} relationship expansion version mismatch`);
  }
  if ((manifest.result.packing === null) !== (manifest.versions.packing_version === null)) {
    throw new Error(`${name} packing summary/version mismatch`);
  }
  if (manifest.result.packing !== null && manifest.result.packing.version !== manifest.versions.packing_version) {
    throw new Error(`${name} packing summary version mismatch`);
  }
  manifest.items.forEach((value) => {
    const candidateFields = [
      value.admission, value.evidence, value.relevance_score, value.utility_score,
      value.packing_reason,
    ];
    if (profileName === "legacy") {
      if ([...candidateFields, value.relationship].some((entry) => entry !== null)) {
        throw new Error(`${name} legacy item carries candidate profile facts`);
      }
    } else if (candidateFields.some((entry) => entry === null)) {
      throw new Error(`${name} candidate item lacks exact V2 or packing facts`);
    }
    if (value.admission !== null && value.admission.profile !== profileName) {
      throw new Error(`${name} item admission profile mismatch`);
    }
    if (value.relationship !== null && value.relationship.version !== manifest.versions.relationship_relevance_version) {
      throw new Error(`${name} item relationship version mismatch`);
    }
  });
}

export function validateManifest(name, manifest) {
  exact(manifest, ["schema", "schema_version", "canonicalization", "mode", "subject", "request", "versions", "result", "packet", "items"], name);
  if (manifest.schema !== SCHEMA || manifest.schema_version !== SCHEMA_VERSION) throw new Error(`${name} unsupported semantic schema`);
  if (manifest.canonicalization !== "rfc8785" || manifest.mode !== "semantic") throw new Error(`${name} protocol mismatch`);
  if (manifest.versions.manifest_contract_version !== CONTRACT_VERSION) throw new Error(`${name} unsupported contract`);
  requireSha256(manifest.request.query_digest, `${name}.query_digest`);
  requireSha256(manifest.request.request_digest, `${name}.request_digest`);
  requireSha256(manifest.packet.hash, `${name}.packet.hash`);
  if (!Array.isArray(manifest.items) || manifest.items.length !== manifest.result.item_count) throw new Error(`${name} item count mismatch`);
  manifest.items.forEach((value, index) => {
    if (value.ordinal !== index) throw new Error(`${name} item ordinal mismatch`);
    requireSha256(value.served_content_hash, `${name}.served_content_hash`);
    if (value.admission !== null) admission(value.admission, `${name}.admission`);
    if (value.evidence !== null) evidence(value.evidence, `${name}.evidence`);
    if (value.relationship !== null) relationship(value.relationship, `${name}.relationship`);
    enumValue(value.review_status, REVIEW_STATUSES, `${name}.review_status`);
    if (value.warning_codes !== null) enumList(value.warning_codes, WARNING_CODES, `${name}.warning_codes`);
    optionalEnum(value.conflict_type, CONFLICT_TYPES, `${name}.conflict_type`);
    optionalEnum(value.conflict_resolution_status, CONFLICT_RESOLUTION_STATUSES, `${name}.conflict_resolution_status`);
    optionalEnum(value.packing_reason, PACKING_REASONS, `${name}.packing_reason`);
  });
  if (manifest.result.expansion !== null) expansion(manifest.result.expansion, `${name}.expansion`);
  if (manifest.result.packing !== null) {
    packing(manifest.result.packing, `${name}.packing`);
    if (manifest.result.packing.selected_count !== manifest.items.length) throw new Error(`${name} packing selected count mismatch`);
  }
  validateProfileCoherence(name, manifest);
  canonicalize(manifest);
  return manifest;
}

export function manifestHash(manifest) {
  return sha256Hex(canonicalize(manifest));
}
