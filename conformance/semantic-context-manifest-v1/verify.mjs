import { readdir, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
  buildManifestFromInput,
  canonicalize,
  manifestHash,
  validateManifest,
} from "./lib.mjs";

const root = dirname(fileURLToPath(import.meta.url));
const vectorDir = join(root, "vectors");
const paths = (await readdir(vectorDir)).filter((name) => name.endsWith(".json")).sort();
if (paths.length < 10) throw new Error(`expected at least 10 vectors, found ${paths.length}`);
for (const path of paths) {
  const vector = JSON.parse(await readFile(join(vectorDir, path), "utf8"));
  const rebuilt = buildManifestFromInput(vector.name, vector.input);
  validateManifest(vector.name, vector.expected.manifest);
  if (canonicalize(rebuilt) !== vector.expected.canonical_json) throw new Error(`${path}: canonical JSON mismatch`);
  if (canonicalize(rebuilt) !== canonicalize(vector.expected.manifest)) throw new Error(`${path}: manifest mismatch`);
  if (manifestHash(rebuilt) !== vector.expected.manifest_hash) throw new Error(`${path}: manifest hash mismatch`);
  if (rebuilt.packet.hash !== vector.expected.packet_hash) throw new Error(`${path}: packet hash mismatch`);
  if (rebuilt.request.query_digest !== vector.expected.query_digest) throw new Error(`${path}: query digest mismatch`);
  if (rebuilt.request.request_digest !== vector.expected.request_digest) throw new Error(`${path}: request digest mismatch`);
  const hashes = rebuilt.items.map((item) => item.served_content_hash);
  if (canonicalize(hashes) !== canonicalize(vector.expected.served_content_hashes)) throw new Error(`${path}: item hashes mismatch`);
}
console.log(`Verified ${paths.length} semantic-context-manifest-v1 vectors (JavaScript).`);
