import { describe, expect, it } from "vitest";

/**
 * API route handler tests.
 *
 * The /api/health route is pure and importable. The /api/ready route depends on
 * the server-only control-plane helper; we exercise its 503 path by temporarily
 * unsetting CONTROL_PLANE_URL (no client can be built).
 */
describe("Portal API", () => {
  it("/api/health returns ok without a database", async () => {
    const mod = await import("../app/api/health/route.js");
    const res = await mod.GET();
    expect(res.status).toBe(200);
    const body = (await res.json()) as { status: string; service: string };
    expect(body.status).toBe("ok");
    expect(body.service).toBe("engram-portal");
  });

  it("/api/ready returns 503 when CONTROL_PLANE_URL is unset", async () => {
    const previous = process.env.CONTROL_PLANE_URL;
    delete process.env.CONTROL_PLANE_URL;
    try {
      const mod = await import("../app/api/ready/route.js");
      const res = await mod.GET();
      expect(res.status).toBe(503);
    } finally {
      if (previous !== undefined) process.env.CONTROL_PLANE_URL = previous;
    }
  });
});
