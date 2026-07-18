// Independent JavaScript verifier for the ENG-CONTEXT-001 shared negative
// conformance fixtures.
//
// Node standard library only (no `npm install`). Each fixture in ./negative
// carries a valid base `input` with exactly one field mutated to violate the
// v1 contract. The JavaScript verifier must REJECT every fixture before a
// manifest is constructed.
//
// This is the JavaScript half of the cross-language rejection proof. The
// Python verifier (scripts/verify_context_manifest_negatives.py) must reject
// the same fixtures. Exact error text need not match across languages, but each
// verifier reports the fixture name and the rejected invariant.
//
// Usage:  node conformance/context-manifest-v1/verify_negatives.mjs

import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { buildManifestFromInput } from "./lib.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const NEGATIVES_DIR = path.join(__dirname, "negative");

let failures = 0;

async function main() {
  let files;
  try {
    files = (await readdir(NEGATIVES_DIR)).filter((f) => f.endsWith(".json")).sort();
  } catch (e) {
    console.error(`no negative fixtures found in ${NEGATIVES_DIR}: ${e.message}`);
    process.exit(1);
  }
  if (files.length === 0) {
    console.error(`no negative fixtures found in ${NEGATIVES_DIR}`);
    process.exit(1);
  }
  console.log(`Verifying ${files.length} negative fixtures (JavaScript)...`);
  for (const file of files) {
    const fixture = JSON.parse(
      await readFile(path.join(NEGATIVES_DIR, file), "utf8")
    );
    const name = fixture.name;
    const expectedError = fixture.expected_error || "";
    try {
      buildManifestFromInput(name, fixture.input);
    } catch (e) {
      // Accepted: a negative fixture must be rejected. Report the invariant
      // token so cross-language disagreement is visible.
      const detail = e.message.split("\n")[0].slice(0, 90);
      console.log(`  OK  ${file}: rejected (${detail})`);
      continue;
    }
    // The fixture was NOT rejected — that is a failure.
    console.error(
      `FAIL ${file}: input was accepted (expected rejection on '${expectedError}')`
    );
    failures += 1;
  }
  if (failures > 0) {
    console.error(`\n${failures} negative fixture(s) were NOT rejected.`);
    process.exit(1);
  }
  console.log(`All ${files.length} negative fixtures rejected.`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
