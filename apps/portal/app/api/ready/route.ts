import { NextResponse } from "next/server";
import { getControlPlaneClientSafe } from "@/lib/control-plane";

/**
 * Portal readiness — ready only when the Control Plane metadata endpoint is
 * reachable. Server-side only; the internal Control Plane URL is never exposed.
 */
export async function GET() {
  const client = getControlPlaneClientSafe();
  if (!client) {
    return NextResponse.json(
      { status: "not_ready", detail: "control_plane_not_configured" },
      { status: 503 },
    );
  }
  try {
    const meta = await client.getMeta();
    return NextResponse.json({ status: "ready", control_plane: meta.service });
  } catch {
    return NextResponse.json(
      { status: "not_ready", detail: "control_plane_unreachable" },
      { status: 503 },
    );
  }
}
