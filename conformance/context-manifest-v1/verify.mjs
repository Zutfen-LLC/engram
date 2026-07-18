// Independent JavaScript verifier for the ENG-CONTEXT-001 golden vectors.
//
// Uses ONLY the Node standard library (no `npm install`). It independently:
//   - implements RFC 8785 JSON Canonicalization Scheme (JCS) canonicalization;
//   - computes SHA-256 over UTF-8 bytes via node:crypto;
//   - re-derives manifest_hash, packet_hash, request_digest, and per-item
//     served_content_hash from each vector's inputs;
//   - verifies every checked-in golden vector.
//
// It NEVER invokes the Python implementation. The Python verifier
// (scripts/verify_context_manifest_vectors.py) and this one must agree on
// every frozen expected value.
//
// Usage:  node conformance/context-manifest-v1/verify.mjs

import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const VECTORS_DIR = path.join(__dirname, "vectors");

// ─── SHA-256 helper ────────────────────────────────────────────────────
// Returns "sha256:<64 lowercase hex>" for the exact UTF-8 bytes of `s`.
// No normalization is applied — exact bytes only. The empty string hashes
// to the SHA-256 of zero bytes (e3b0c442...).
function sha256Hex(s) {
  return "sha256:" + createHash("sha256").update(s, "utf8").digest("hex");
}

// ─── RFC 8785 (JCS) canonicalization ───────────────────────────────────
// Independent implementation. Semantics per RFC 8785:
//   - UTF-8 encoded, no BOM, no insignificant whitespace.
//   - Object members ordered by UTF-16 code unit of the member name.
//   - Arrays preserve order.
//   - JSON string escaping: ECMAScript-compatible. Non-ASCII (>= U+0080)
//     characters are NOT escaped (preserved as their exact Unicode scalars).
//   - Number serialization: ECMAScript Number.prototype.toString(), with the
//     additional JCS rule that -0 is serialized as "0". NaN and +Infinity
//     / -Infinity are rejected (they are not valid JSON).
//
// JavaScript's default string `<` comparison orders by UTF-16 code unit, and
// `JSON.stringify(string)` produces ECMAScript-compatible escaping without
// escaping non-ASCII — both of which match JCS. This is why JCS is natural to
// implement in JS and why `json.dumps(sort_keys=True)` in Python (Unicode
// code-point ordering, no JCS number format) is NOT equivalent.

function canonicalize(value) {
  // Returns a string (the canonical JSON text).
  if (value === null) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return canonicalString(value);
  if (typeof value === "number") return canonicalNumber(value);
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalize).join(",") + "]";
  }
  if (typeof value === "object") {
    // Order keys by UTF-16 code unit (JS default string comparison).
    const keys = Object.keys(value).sort();
    const members = keys.map((k) => canonicalString(k) + ":" + canonicalize(value[k]));
    return "{" + members.join(",") + "}";
  }
  throw new Error(`JCS: unsupported value type: ${typeof value}`);
}

function canonicalString(s) {
  // JSON.stringify on a single string yields the ECMAScript-compatible quoted
  // form (escapes control chars, ", \; does NOT escape non-ASCII). JCS wants
  // exactly this. (For U+2028/U+2029 JSON.stringify does not escape them,
  // which is also what JCS requires.)
  return JSON.stringify(s);
}

function canonicalNumber(n) {
  if (Number.isNaN(n)) {
    throw new Error("JCS: NaN is not valid JSON and must be rejected");
  }
  if (!Number.isFinite(n)) {
    throw new Error("JCS: Infinity/-Infinity is not valid JSON and must be rejected");
  }
  // JCS §3.2.2.3: -0 is serialized as "0". Number.prototype.toString(-0)
  // yields "0" already, but be explicit and defensive.
  if (Object.is(n, -0)) return "0";
  return n.toString();
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
  if (expected !== got) {
    fail(name, what, expected, got);
  }
}

async function verifyVector(file) {
  const raw = await readFile(path.join(VECTORS_DIR, file), "utf8");
  const vector = JSON.parse(raw);
  const name = vector.name;
  const inp = vector.input;
  const exp = vector.expected;

  // 1. manifest_hash: independently canonicalize the frozen expected.manifest
  //    object and SHA-256 it. This proves the frozen manifest object hashes
  //    to the frozen manifest_hash without touching Python.
  const manifestCanon = canonicalize(exp.manifest);
  const recomputedManifestHash = sha256Hex(manifestCanon);
  assertEqual(name, "manifest_hash", exp.manifest_hash, recomputedManifestHash);

  // 2. The frozen canonical_json must equal our independent canonicalization.
  if (manifestCanon !== exp.canonical_json) {
    fail(name, "canonical_json bytes", exp.canonical_json, manifestCanon);
  }

  // 3. The SHA-256 of the frozen canonical_json bytes (as UTF-8) must equal
  //    the frozen manifest_hash. (Independent re-derivation from bytes.)
  assertEqual(
    name,
    "canonical_json byte hash",
    exp.manifest_hash,
    sha256Hex(exp.canonical_json)
  );

  // 4. packet_hash: independently hash the exact UTF-8 bytes of
  //    response.working_set.
  const packetHash = sha256Hex(inp.response.working_set);
  assertEqual(name, "packet_hash", exp.packet_hash, packetHash);

  // 5. per-item served_content_hash: independently hash each served content's
  //    exact UTF-8 bytes.
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
    const h = sha256Hex(items[i].content);
    assertEqual(name, `served_content_hash[${i}]`, exp.served_content_hashes[i], h);
  }

  // 6. request_digest: independently canonicalize the request descriptor
  //    (without request_digest) and hash it. We reconstruct the pre-digest
  //    descriptor from the frozen request_context input.
  const reqDescriptor = {
    requested: inp.request_context.requested,
    effective: inp.request_context.effective,
    query_digest: inp.request_context.query_digest,
  };
  const requestDigest = sha256Hex(canonicalize(reqDescriptor));
  assertEqual(name, "request_digest", exp.request_digest, requestDigest);

  // 7. Key-order independence: re-canonicalize the manifest with its top-level
  //    keys in a different insertion order — bytes must be identical (proves
  //    member-ordering is canonical, not insertion-order, for vector 007 and
  //    as a general invariant).
  const reordered = {};
  for (const k of Object.keys(exp.manifest).reverse()) {
    reordered[k] = exp.manifest[k];
  }
  const reorderedCanon = canonicalize(reordered);
  if (reorderedCanon !== manifestCanon) {
    fail(name, "key-order independence", manifestCanon, reorderedCanon);
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
