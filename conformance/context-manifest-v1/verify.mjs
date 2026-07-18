// Independent JavaScript verifier for the ENG-CONTEXT-001 golden vectors.
//
// Uses ONLY the Node standard library (no `npm install`). It independently:
//   - implements RFC 8785 JSON Canonicalization Scheme (JCS) canonicalization;
//   - computes SHA-256 over UTF-8 bytes via node:crypto;
//   - validates response coherence (item count, byte count, working-set-v1
//     render) and rejects incoherent input BEFORE reconstruction;
//   - reconstructs the COMPLETE manifest object from `input` using the v1
//     contract (subject, request + digest, versions, result counts, packet
//     descriptor, ordered item snapshots, per-item content hashes);
//   - compares the reconstructed canonical object against expected.manifest,
//     expected.canonical_json, and expected.manifest_hash;
//   - independently re-derives packet_hash, request_digest, and per-item
//     served_content_hash from the frozen inputs.
//
// It NEVER invokes the Python implementation and does not merely hash
// expected.manifest — it rebuilds the manifest from the inputs. The Python
// verifier (scripts/verify_context_manifest_vectors.py) and this one must
// agree on every frozen expected value.
//
// Usage:  node conformance/context-manifest-v1/verify.mjs

import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const VECTORS_DIR = path.join(__dirname, "vectors");

// ─── Stable contract constants (must match the Python model exactly) ───
const SCHEMA = "engram.context-manifest";
const SCHEMA_VERSION = "1.0";
const CANONICALIZATION = "rfc8785";
const MODE = "startup";
const MEMORY_CONTEXT_VERSION = "memory-context-v2";
const MANIFEST_CONTRACT_VERSION = "context-manifest-v1";
const PACKET_RENDER_VERSION = "working-set-v1";
const PACKET_MEDIA_TYPE = "text/plain; charset=utf-8";
const VISIBILITY_VALUES = new Set(["private", "workspace", "tenant", "public"]);

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SHA256_RE = /^sha256:[0-9a-f]{64}$/;

// ─── SHA-256 helper ────────────────────────────────────────────────────
function sha256Hex(s) {
  return "sha256:" + createHash("sha256").update(s, "utf8").digest("hex");
}

// ─── RFC 8785 (JCS) canonicalization ───────────────────────────────────
function canonicalize(value) {
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return canonicalString(value);
  if (typeof value === "number") return canonicalNumber(value);
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalize).join(",") + "]";
  }
  if (typeof value === "object") {
    const keys = Object.keys(value).sort(); // UTF-16 code unit order (JCS)
    const members = keys.map((k) => canonicalString(k) + ":" + canonicalize(value[k]));
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

// ─── Strict input validation helpers (mirror the Python builder) ───────
function isStrictBool(v) {
  return typeof v === "boolean";
}
function isStrictInt(v) {
  return typeof v === "number" && Number.isInteger(v) && !Object.is(v, -0) === true && !isStrictBool(v) && Number.isFinite(v);
}
function isStrictStr(v) {
  return typeof v === "string";
}
function isFiniteNumber(v) {
  return (
    (typeof v === "number") && Number.isFinite(v) && !isStrictBool(v)
  );
}

function requireStr(v, where) {
  if (!isStrictStr(v)) throw new Error(`${where} must be a string, got ${typeof v}`);
  return v;
}
function requireInt(v, where) {
  if (isStrictBool(v) || typeof v !== "number" || !Number.isInteger(v)) {
    throw new Error(`${where} must be an integer (not bool), got ${typeof v}`);
  }
  return v;
}
function requireBool(v, where) {
  if (!isStrictBool(v)) throw new Error(`${where} must be a boolean, got ${typeof v}`);
  return v;
}
function requireFiniteFloat(v, where) {
  if (isStrictBool(v) || typeof v !== "number" || !Number.isFinite(v)) {
    throw new Error(`${where} must be a finite number, got ${typeof v}`);
  }
  return v;
}
function requireStrList(v, where) {
  if (!Array.isArray(v)) throw new Error(`${where} must be a list`);
  for (const el of v) {
    if (!isStrictStr(el)) throw new Error(`${where} must contain only strings`);
  }
  return v;
}

// ─── Response coherence (reject incoherent input before reconstruction) ─
function reconstructWorkingSetV1(items) {
  return items.map((i) => `[${i.kind}] ${i.content}`).join("\n");
}

function assertResponseCoherence(name, response) {
  if (!Array.isArray(response.items)) {
    throw new Error(`${name}: response.items must be an array`);
  }
  const itemCount = requireInt(response.item_count, `${name}: response.item_count`);
  if (itemCount !== response.items.length) {
    throw new Error(
      `${name}: response.item_count (${itemCount}) != len(items) (${response.items.length})`
    );
  }
  const byteCount = requireInt(response.byte_count, `${name}: response.byte_count`);
  let derived = 0;
  for (const it of response.items) {
    if (!isStrictStr(it.content)) {
      throw new Error(`${name}: response item content must be a string`);
    }
    derived += Buffer.byteLength(it.content, "utf8");
  }
  if (byteCount !== derived) {
    throw new Error(
      `${name}: response.byte_count (${byteCount}) != derived (${derived})`
    );
  }
  if (!isStrictStr(response.working_set)) {
    throw new Error(`${name}: response.working_set must be a string`);
  }
  const reconstructed = reconstructWorkingSetV1(response.items);
  if (response.working_set !== reconstructed) {
    throw new Error(
      `${name}: response.working_set does not match the working-set-v1 render of items`
    );
  }
}

// ─── Full manifest reconstruction from input ───────────────────────────
function buildManifestFromInput(name, inp) {
  const subject = inp.subject_context;
  const request = inp.request_context;
  const versions = inp.decision_versions;
  const response = inp.response;

  // Validate stable markers on input (the reconstruction trusts the frozen
  // inputs, but these must match the v1 contract).
  if (subject.memory_context_version !== MEMORY_CONTEXT_VERSION) {
    throw new Error(`${name}: subject.memory_context_version mismatch`);
  }
  if (versions.manifest_contract_version !== MANIFEST_CONTRACT_VERSION) {
    throw new Error(`${name}: versions.manifest_contract_version mismatch`);
  }
  if (versions.packet_render_version !== PACKET_RENDER_VERSION) {
    throw new Error(`${name}: versions.packet_render_version mismatch`);
  }

  // Startup-context coherence.
  if (request.query_digest !== null) {
    throw new Error(`${name}: startup query_digest must be null`);
  }
  if (subject.workspace_id !== request.effective.workspace_id) {
    throw new Error(`${name}: subject/effective workspace_id mismatch`);
  }

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
      item_id: requireStr(raw.id, `${name}: item id`),
      kind: requireStr(raw.kind, `${name}: item kind`),
      served_content_hash: sha256Hex(content),
      review_status: requireStr(raw.review_status, `${name}: item review_status`),
      authority: requireInt(raw.authority, `${name}: item authority`),
      visibility: requireStr(raw.visibility, `${name}: item visibility`),
      workspace_id: raw.workspace_id === null ? null : requireStr(raw.workspace_id, `${name}: item workspace_id`),
      score: raw.score === null ? null : requireFiniteFloat(raw.score, `${name}: item score`),
      reasons: requireStrList(raw.reasons, `${name}: item reasons`),
      warnings: requireStrList(raw.warnings, `${name}: item warnings`),
      pinned: requireBool(raw.pinned, `${name}: item pinned`),
      importance: requireFiniteFloat(raw.importance, `${name}: item importance`),
      source_trust: requireFiniteFloat(raw.source_trust, `${name}: item source_trust`),
      memory_confidence: requireFiniteFloat(raw.memory_confidence, `${name}: item memory_confidence`),
      human_verified: requireBool(raw.human_verified, `${name}: item human_verified`),
      conflict_type: raw.conflict_type === null ? null : requireStr(raw.conflict_type, `${name}: item conflict_type`),
      conflict_resolution_status:
        raw.conflict_resolution_status === null
          ? null
          : requireStr(raw.conflict_resolution_status, `${name}: item conflict_resolution_status`),
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

// ─── Vector verification ───────────────────────────────────────────────

let failures = 0;

function fail(name, what, expected, got) {
  console.error(`FAIL ${name}: ${what}`);
  console.error(`  expected: ${expected}`);
  console.error(`  got:      ${got}`);
  failures += 1;
}

function assertEqual(name, what, expected, got) {
  if (expected !== got) fail(name, what, expected, got);
}

function assertDeepEqual(name, what, expected, got) {
  const e = canonicalize(expected);
  const g = canonicalize(got);
  if (e !== g) {
    fail(name, what + " (canonical)", e, g);
  }
}

async function verifyVector(file) {
  const raw = await readFile(path.join(VECTORS_DIR, file), "utf8");
  const vector = JSON.parse(raw);
  const name = vector.name;
  const inp = vector.input;
  const exp = vector.expected;

  // A) Reconstruct the COMPLETE manifest from input independently, then
  //    compare to expected.manifest. This is the core reconstruction check.
  let reconstructed;
  try {
    reconstructed = buildManifestFromInput(name, inp);
  } catch (e) {
    fail(name, "reconstruction", "(should succeed)", e.message);
    return;
  }
  assertDeepEqual(name, "reconstructed manifest", exp.manifest, reconstructed);

  // B) Reconstructed manifest canonicalizes to expected.canonical_json.
  const reconCanon = canonicalize(reconstructed);
  if (reconCanon !== exp.canonical_json) {
    fail(name, "reconstructed canonical_json", exp.canonical_json, reconCanon);
  }

  // C) Reconstructed manifest hashes to expected.manifest_hash.
  assertEqual(
    name,
    "reconstructed manifest_hash",
    exp.manifest_hash,
    sha256Hex(reconCanon)
  );

  // D) The frozen expected.manifest also canonicalizes to expected.canonical_json.
  const frozenCanon = canonicalize(exp.manifest);
  if (frozenCanon !== exp.canonical_json) {
    fail(name, "frozen canonical_json", exp.canonical_json, frozenCanon);
  }

  // E) SHA-256 of the frozen canonical_json bytes equals expected.manifest_hash.
  assertEqual(
    name,
    "frozen canonical_json byte hash",
    exp.manifest_hash,
    sha256Hex(exp.canonical_json)
  );

  // F) packet_hash: independently hash exact UTF-8 bytes of working_set.
  assertEqual(
    name,
    "packet_hash",
    exp.packet_hash,
    sha256Hex(inp.response.working_set)
  );

  // G) request_digest: independently recompute over the canonical descriptor.
  const reqDescriptor = {
    requested: inp.request_context.requested,
    effective: inp.request_context.effective,
    query_digest: inp.request_context.query_digest,
  };
  assertEqual(
    name,
    "request_digest",
    exp.request_digest,
    sha256Hex(canonicalize(reqDescriptor))
  );

  // H) per-item served_content_hash from served content bytes.
  const items = inp.response.items;
  if (exp.served_content_hashes.length !== items.length) {
    fail(
      name,
      "served_content_hashes length",
      String(exp.served_content_hashes.length),
      String(items.length)
    );
  }
  for (let i = 0; i < items.length; i++) {
    assertEqual(
      name,
      `served_content_hash[${i}]`,
      exp.served_content_hashes[i],
      sha256Hex(items[i].content)
    );
  }

  // I) Key-order independence: re-canonicalize with reversed top-level keys.
  const reordered = {};
  for (const k of Object.keys(exp.manifest).reverse()) {
    reordered[k] = exp.manifest[k];
  }
  if (canonicalize(reordered) !== frozenCanon) {
    fail(name, "key-order independence", frozenCanon, canonicalize(reordered));
  }

  if (failures === 0) {
    console.log(
      `  OK  ${file}: manifest_hash=${exp.manifest_hash.slice(0, 24)}...`
    );
  }
}

async function main() {
  let files;
  try {
    files = (await readdir(VECTORS_DIR)).filter((f) => f.endsWith(".json")).sort();
  } catch (e) {
    console.error(`no vectors found in ${VECTORS_DIR}: ${e.message}`);
    process.exit(1);
  }
  if (files.length === 0) {
    console.error(`no vectors found in ${VECTORS_DIR}`);
    process.exit(1);
  }
  console.log(`Verifying ${files.length} context-manifest-v1 vectors (JavaScript)...`);
  for (const file of files) {
    await verifyVector(file);
  }
  if (failures > 0) {
    console.error(`\n${failures} verification failure(s).`);
    process.exit(1);
  }
  console.log(`All ${files.length} vectors verified.`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
