/**
 * engram-portal-sdk — server-side client + generated types for the Control Plane.
 *
 * Re-exports the handwritten client and the generated OpenAPI types. The
 * generated types are the single source of truth for Control Plane responses.
 */
export {
  ControlPlaneClient,
  ControlPlaneError,
  createControlPlaneClient,
} from "./client";
export type { ControlPlaneClientOptions } from "./client";
export type {
  components as OpenApiComponents,
  paths as OpenApiPaths,
} from "./generated/openapi";
