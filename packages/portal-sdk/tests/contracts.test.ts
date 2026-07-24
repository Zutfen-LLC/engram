import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Contract drift guard: the committed generated TS types must match what the
 * generator produces from the committed OpenAPI. This mirrors the CI
 * `contracts:check` step so drift fails locally too.
 */
describe("portal-sdk contracts", () => {
  it("generated TS types are current (no drift)", () => {
    const here = new URL(".", import.meta.url).pathname;
    const script = resolve(here, "..", "scripts", "generate.ts");
    expect(() => {
      execFileSync("pnpm", ["exec", "tsx", script, "--check"], {
        stdio: "pipe",
      });
    }).not.toThrow();
  });
});
