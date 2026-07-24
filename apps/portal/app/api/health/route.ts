import { NextResponse } from "next/server";

/**
 * Portal liveness — database-free.
 *
 * Mirrors the Control Plane /health contract: a stable, dependency-free 200.
 */
export async function GET() {
  return NextResponse.json({ status: "ok", service: "engram-portal" });
}
