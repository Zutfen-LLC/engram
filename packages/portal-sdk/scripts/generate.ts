/**
 * Generate TypeScript types from the committed Control Plane OpenAPI contract.
 *
 * Source of truth: services/control-plane (engram-control-plane export-openapi),
 * materialized to packages/portal-sdk/openapi.json. This script generates
 * packages/portal-sdk/src/generated/openapi.ts from that JSON via
 * openapi-typescript.
 *
 * Drift check: `pnpm contracts:check` regenerates to a temp string and
 * byte-compares against the committed file (mirrors the repo's Python --check
 * pattern). Generated files carry a header.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString } from "openapi-typescript";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const OPENAPI_JSON = resolve(ROOT, "openapi.json");
const OUT = resolve(ROOT, "src/generated/openapi.ts");
const HEADER = `// GENERATED CODE — DO NOT EDIT.
// Source: packages/portal-sdk/openapi.json (Control Plane OpenAPI contract).
// Regenerate with \`pnpm --filter engram-portal-sdk contracts:generate\`.
`;

async function generate(): Promise<string> {
  if (!existsSync(OPENAPI_JSON)) {
    throw new Error(
      `Missing ${OPENAPI_JSON}. Run \`engram-control-plane export-openapi -o packages/portal-sdk/openapi.json\` first.`,
    );
  }
  const spec = JSON.parse(readFileSync(OPENAPI_JSON, "utf8"));
  const ast = await openapiTS(spec);
  return HEADER + astToString(ast) + "\n";
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes("--check")) {
    const generated = await generate();
    if (!existsSync(OUT)) {
      console.error(`DRIFT: src/generated/openapi.ts does not exist. Regenerate with: pnpm --filter engram-portal-sdk contracts:generate`);
      process.exit(1);
    }
    const committed = readFileSync(OUT, "utf8");
    if (committed !== generated) {
      console.error(`DRIFT: src/generated/openapi.ts does not match the generated types. Regenerate with: pnpm --filter engram-portal-sdk contracts:generate`);
      process.exit(1);
    }
    console.log("OK: src/generated/openapi.ts is current.");
    return;
  }
  const generated = await generate();
  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, generated, "utf8");
  console.log(`wrote ${resolve(ROOT, "src/generated/openapi.ts").split("/packages/portal-sdk/")[1]}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
