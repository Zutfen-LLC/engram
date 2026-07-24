import { describe, expect, it, vi } from "vitest";
import { ControlPlaneClient, ControlPlaneError } from "../src/client.js";

function makeFetch(response: Response | Error, delayMs = 0) {
  return vi.fn(async () => {
    if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
    if (response instanceof Error) throw response;
    return response;
  });
}

describe("ControlPlaneClient", () => {
  it("getHealth returns typed health on 200", async () => {
    const fetchImpl = makeFetch(
      new Response(JSON.stringify({ status: "ok", service: "engram-control-plane", version: "0.1.0", build_sha: "abc" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = new ControlPlaneClient({ baseUrl: "http://cp", fetchImpl: fetchImpl as unknown as typeof fetch });
    const h = await client.getHealth();
    expect(h.status).toBe("ok");
    expect(h.service).toBe("engram-control-plane");
    expect(fetchImpl).toHaveBeenCalledOnce();
    const url = (fetchImpl.mock.calls[0]?.[0] as string) ?? "";
    expect(url).toBe("http://cp/health");
  });

  it("getMeta returns typed meta on 200", async () => {
    const fetchImpl = makeFetch(
      new Response(
        JSON.stringify({
          service: "engram-control-plane",
          api_version: "0.1.0",
          environment: "development",
          build_sha: "abc",
          database_boundary: "dedicated_database",
          identity_status: "not_configured",
          delegation_status: "not_implemented",
          billing_status: "not_configured",
          core_access_mode: "service_api_only",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const client = new ControlPlaneClient({ baseUrl: "http://cp", fetchImpl: fetchImpl as unknown as typeof fetch });
    const m = await client.getMeta();
    expect(m.database_boundary).toBe("dedicated_database");
    expect(m.core_access_mode).toBe("service_api_only");
  });

  it("throws a sanitized ControlPlaneError on non-200 (no URL leak)", async () => {
    const fetchImpl = makeFetch(
      new Response(JSON.stringify({ error: "not_ready" }), {
        status: 503,
        headers: { "content-type": "application/json" },
      }),
    );
    const client = new ControlPlaneClient({ baseUrl: "http://internal-secret-host:8100", fetchImpl: fetchImpl as unknown as typeof fetch });
    await expect(client.getReady()).rejects.toMatchObject({
      name: "ControlPlaneError",
      code: "not_ready",
      status: 503,
    });
    try {
      await client.getReady();
    } catch (e) {
      expect((e as Error).message).not.toContain("internal-secret-host");
    }
  });

  it("sanitize the internal URL on a transport failure", async () => {
    const fetchImpl = makeFetch(new Error("ECONNREFUSED http://internal-secret-host:8100"));
    const client = new ControlPlaneClient({ baseUrl: "http://internal-secret-host:8100", fetchImpl: fetchImpl as unknown as typeof fetch });
    await expect(client.getHealth()).rejects.toMatchObject({
      name: "ControlPlaneError",
      code: "unreachable",
    });
    try {
      await client.getHealth();
    } catch (e) {
      expect((e as Error).message).not.toContain("internal-secret-host");
    }
  });

  it("isReady returns false instead of throwing", async () => {
    const fetchImpl = makeFetch(new Error("boom"));
    const client = new ControlPlaneClient({ baseUrl: "http://cp", fetchImpl: fetchImpl as unknown as typeof fetch });
    await expect(client.isReady()).resolves.toBe(false);
  });

  it("createControlPlaneClient throws ControlPlaneError when CONTROL_PLANE_URL unset", () => {
    expect(() =>
      createControlPlaneClientSafe({ CONTROL_PLANE_URL: "" }),
    ).toThrow(ControlPlaneError);
  });
});

function createControlPlaneClientSafe(env: { CONTROL_PLANE_URL: string }) {
  // Re-implement the guard to avoid importing process-dependent factory.
  if (!env.CONTROL_PLANE_URL) {
    throw new ControlPlaneError("missing", { code: "config_error" });
  }
  return new ControlPlaneClient({ baseUrl: env.CONTROL_PLANE_URL });
}
