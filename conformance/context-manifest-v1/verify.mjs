// Independent JavaScript verifier for the ENG-CONTEXT-001 golden vectors.
//
// Uses ONLY the Node standard library (no `npm install`). It independently
// reconstructs each vector's manifest from its frozen inputs and re-derives
// every expected hash, then compares against the frozen expected values. The
// shared contract library (./lib.mjs) is the single source of truth for the v1
// contract as enforced by JavaScript; this runner drives it over the positive
// golden vectors. The negative-fixture runner is ./verify_negatives.mjs.
//
// It NEVER invokes the Python implementation and does not merely hash
// expected.manifest — it rebuilds the manifest from the inputs. The Python
// verifier (scripts/verify_context_manifest_vectors.py) and this one must
// agree on every frozen expected value.
//
// Usage:  node conformance/context-manifest-v1/verify.mjs

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  assertPostReconstructionInvariants,
  buildManifestFromInput,
  canonicalize,
  sha256Hex,
  validateExpectedManifest,
} from "./lib.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const VECTORS_DIR = path.join(__dirname, "vectors");

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

  // B) Independently validate the frozen expected manifest BEFORE comparison.
  try {
    validateExpectedManifest(name, exp.manifest);
  } catch (e) {
    fail(name, "expected.manifest validation", "(should be conformant)", e.message);
  }

  // C) Post-reconstruction semantic invariants over the rebuilt manifest.
  try {
    assertPostReconstructionInvariants(name, reconstructed, inp.response);
  } catch (e) {
    fail(name, "post-reconstruction invariants", "(should hold)", e.message);
  }

  assertDeepEqual(name, "reconstructed manifest", exp.manifest, reconstructed);

  // D) Reconstructed manifest canonicalizes to expected.canonical_json.
  const reconCanon = canonicalize(reconstructed);
  if (reconCanon !== exp.canonical_json) {
    fail(name, "reconstructed canonical_json", exp.canonical_json, reconCanon);
  }

  // E) Reconstructed manifest hashes to expected.manifest_hash.
  assertEqual(
    name,
    "reconstructed manifest_hash",
    exp.manifest_hash,
    sha256Hex(reconCanon)
  );

  // F) The frozen expected.manifest also canonicalizes to expected.canonical_json.
  const frozenCanon = canonicalize(exp.manifest);
  if (frozenCanon !== exp.canonical_json) {
    fail(name, "frozen canonical_json", exp.canonical_json, frozenCanon);
  }

  // G) SHA-256 of the frozen canonical_json bytes equals expected.manifest_hash.
  assertEqual(
    name,
    "frozen canonical_json byte hash",
    exp.manifest_hash,
    sha256Hex(exp.canonical_json)
  );

  // H) packet_hash: independently hash exact UTF-8 bytes of working_set.
  assertEqual(
    name,
    "packet_hash",
    exp.packet_hash,
    sha256Hex(inp.response.working_set)
  );

  // I) request_digest: independently recompute over the canonical descriptor.
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

  // J) per-item served_content_hash from served content bytes.
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

  // K) Key-order independence: re-canonicalize with reversed top-level keys.
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
