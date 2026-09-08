import { readdir, readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { buildManifestFromInput, canonicalize, validateManifest } from "./lib.mjs";

const root = dirname(fileURLToPath(import.meta.url));
const negativeDir = join(root, "negative");
const paths = (await readdir(negativeDir)).filter((name) => name.endsWith(".json")).sort();
if (paths.length < 48) throw new Error(`expected at least 48 negatives, found ${paths.length}`);
for (const path of paths) {
  let rejected = false;
  try {
    const fixture = JSON.parse(await readFile(join(negativeDir, path), "utf8"));
    if (fixture.parse_as === "startup") {
      if (fixture.manifest.schema !== "engram.context-manifest") throw new Error("not startup");
    } else if (fixture.parse_as === "dispatch") {
      validateManifest(fixture.name, fixture.manifest);
    } else {
      validateManifest(fixture.name, fixture.manifest);
      const rebuilt = buildManifestFromInput(fixture.name, fixture.input);
      if (canonicalize(rebuilt) !== canonicalize(fixture.manifest)) {
        throw new Error("manifest differs from finalized-input reconstruction");
      }
    }
  } catch {
    rejected = true;
  }
  if (!rejected) throw new Error(`${path} was accepted`);
}
console.log(`All ${paths.length} semantic negative fixtures rejected (JavaScript).`);
