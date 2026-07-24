/**
 * Server-side Control Plane client for the Engram Portal.
 *
 * This client is SERVER-SIDE ONLY (ENG-PORTAL-001A). It must never be imported
 * from browser code: the internal Control Plane URL is not exposed to the
 * browser, and no browser credential abstraction exists in this slice.
 *
 * Response types are NOT duplicated here — they come from the generated
 * `./generated/openapi` types, which are the single source of truth derived from
 * the committed Control Plane OpenAPI contract.
 */
import type { components, paths } from "./generated/openapi";

type HealthResponse = components["schemas"]["HealthResponse"];
type ReadyResponse = components["schemas"]["ReadyResponse"];
type MetaResponse = components["schemas"]["MetaResponse"];

/** A sanitized transport error — never exposes the internal URL or headers. */
export class ControlPlaneError extends Error {
  readonly status: number | undefined;
  readonly code: string;
  constructor(message: string, opts: { status?: number; code?: string } = {}) {
    super(message);
    this.name = "ControlPlaneError";
    this.status = opts.status;
    this.code = opts.code ?? "control_plane_error";
  }
}

export interface ControlPlaneClientOptions {
  /** Base URL of the Control Plane (server-side only). Required. */
  baseUrl: string;
  /** Per-request timeout in milliseconds. Default 4000. */
  timeoutMs?: number;
  /**
   * Fetch implementation. Defaults to global fetch. Injected for tests and for
   * Next.js route handlers where an instrumented fetch may be preferred.
   */
  fetchImpl?: typeof fetch;
  /** Extra headers applied to every request (e.g. request-id). */
  headers?: Record<string, string>;
}

const DEFAULT_TIMEOUT_MS = 4000;

export class ControlPlaneClient {
  private readonly baseUrl: string;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly headers: Record<string, string>;

  constructor(opts: ControlPlaneClientOptions) {
    if (!opts.baseUrl) {
      throw new ControlPlaneError("baseUrl is required", { code: "config_error" });
    }
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.headers = { accept: "application/json", ...(opts.headers ?? {}) };
  }

  /** GET /health — database-free liveness. */
  async getHealth(): Promise<HealthResponse> {
    return this.get<HealthResponse>("/health");
  }

  /** GET /ready — 200 only when the dedicated DB is reachable and at head. */
  async getReady(): Promise<ReadyResponse> {
    return this.get<ReadyResponse>("/ready");
  }

  /** GET /control/v1/meta — safe scaffold metadata and boundary contract. */
  async getMeta(): Promise<MetaResponse> {
    return this.get<MetaResponse>("/control/v1/meta");
  }

  /**
   * Probe readiness as a boolean for status rendering. Returns false on any
   * transport error or non-200, never throwing to the caller.
   */
  async isReady(): Promise<boolean> {
    try {
      const r = await this.getReady();
      return r.status === "ready";
    } catch {
      return false;
    }
  }

  private async get<T>(path: keyof paths | string): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let res: Response;
    try {
      res = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "GET",
        headers: this.headers,
        signal: controller.signal,
      });
    } catch (e) {
      // Sanitize: never include the URL, headers, or fetch internals.
      if (controller.signal.aborted) {
        throw new ControlPlaneError("request timed out", { code: "timeout" });
      }
      throw new ControlPlaneError("control plane unreachable", {
        code: "unreachable",
      });
    } finally {
      clearTimeout(timer);
    }

    if (!res.ok) {
      // Body is already sanitized by the Control Plane; surface only the code.
      let code = `http_${res.status}`;
      try {
        const body = (await res.json()) as { error?: string };
        if (body?.error) code = body.error;
      } catch {
        // keep http_<status>
      }
      throw new ControlPlaneError(`control plane returned ${res.status}`, {
        status: res.status,
        code,
      });
    }
    return (await res.json()) as T;
  }
}

/** Convenience factory reading CONTROL_PLANE_URL from the environment. */
export function createControlPlaneClient(
  env: { CONTROL_PLANE_URL: string } = process.env as unknown as {
    CONTROL_PLANE_URL: string;
  },
  opts: Omit<ControlPlaneClientOptions, "baseUrl"> = {},
): ControlPlaneClient {
  const baseUrl = env.CONTROL_PLANE_URL;
  if (!baseUrl) {
    throw new ControlPlaneError(
      "CONTROL_PLANE_URL is not configured (server-side env)",
      { code: "config_error" },
    );
  }
  return new ControlPlaneClient({ baseUrl, ...opts });
}
