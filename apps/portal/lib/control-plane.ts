/**
 * Server-side Control Plane access for the Portal.
 *
 * The internal Control Plane URL comes from CONTROL_PLANE_URL (server-only env,
 * NEVER exposed via NEXT_PUBLIC_*). These helpers are imported only from server
 * components and route handlers. There is deliberately no browser-side wrapper.
 */
import "server-only";
import {
  ControlPlaneClient,
  createControlPlaneClient,
} from "engram-portal-sdk";

/**
 * Return a client, or null when CONTROL_PLANE_URL is unset.
 *
 * The Portal must render an honest "unavailable" state when the Control Plane
 * is absent, rather than crashing — so callers use the null-safe form.
 */
export function getControlPlaneClientSafe(): ControlPlaneClient | null {
  const url = process.env.CONTROL_PLANE_URL;
  if (!url) {
    return null;
  }
  try {
    return createControlPlaneClient({ CONTROL_PLANE_URL: url });
  } catch {
    return null;
  }
}

/** Return a client, throwing if CONTROL_PLANE_URL is unset. */
export function getControlPlaneClient(): ControlPlaneClient {
  return createControlPlaneClient({
    CONTROL_PLANE_URL: process.env.CONTROL_PLANE_URL ?? "",
  });
}
