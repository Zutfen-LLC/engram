#!/usr/bin/env python3
"""Validate the hosted Compose overlay (ENG-PORTAL-001A).

Assertions (mirrors the ``hosted-compose-validate`` CI job):
  1. Base compose + overlay both validate (``docker compose config -q``).
  2. Standard stack WITHOUT the overlay resolves to exactly
     {postgres, engram-service, engram-worker} (non-regression).
  3. All hosted services are present WITH the overlay:
     control-db-bootstrap, control-plane-migrate, control-plane, portal.
  4. Portal has NO database URL of any kind.
  5. Control Plane has NO Core database URL (only engram_control).
  6. No Core API key (ENGRAM_*_API_KEY / ENGRAM_API_KEY) is propagated to Portal.
  7. Control Plane runs as engram_control_app (not the owner).
  8. No NEXT_PUBLIC_* variable references the Control Plane internal URL.

Exit 0 on success, non-zero on any failed assertion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence

ROOT = "docker-compose.yml"
OVERLAY = "docker-compose.hosted.yml"

STANDARD_SERVICES = {"engram-service", "engram-worker", "postgres"}
REQUIRED_HOSTED = {
    "control-db-bootstrap",
    "control-plane-migrate",
    "control-plane",
    "portal",
}


def _run(args: Sequence[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def _resolve(*files: str) -> dict:
    return json.loads(_run(["docker", "compose", *sum([["-f", f] for f in files], []), "config", "--format", "json"]))


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    # 1. Both validate.
    for files in ([ROOT], [ROOT, OVERLAY]):
        subprocess.run(["docker", "compose", *sum([["-f", f] for f in files], []), "config", "-q"], check=True)

    # 2. Standard-stack non-regression.
    standard = set(_run(["docker", "compose", "-f", ROOT, "config", "--services"]).split())
    if standard != STANDARD_SERVICES:
        fail(f"standard stack changed without overlay: {sorted(standard)} != {sorted(STANDARD_SERVICES)}")

    # 3. All hosted services present with overlay.
    with_overlay = set(_run(["docker", "compose", "-f", ROOT, "-f", OVERLAY, "config", "--services"]).split())
    missing = REQUIRED_HOSTED - with_overlay
    if missing:
        fail(f"missing hosted services: {sorted(missing)}")
    extra_core_overlap = STANDARD_SERVICES - (STANDARD_SERVICES & with_overlay)
    if extra_core_overlap:
        fail(f"overlay dropped core services: {sorted(extra_core_overlap)}")

    resolved = _resolve(ROOT, OVERLAY)
    services = resolved["services"]

    portal_env = services["portal"].get("environment", {})
    cp_env = services["control-plane"].get("environment", {})

    # 4. Portal has no database URL.
    portal_env_text = json.dumps(portal_env)
    if "DATABASE_URL" in portal_env_text or "postgres://" in portal_env_text.lower():
        fail("Portal must have no database URL")

    # 5. Control Plane has no Core database URL.
    cp_db = cp_env.get("ENGRAM_CONTROL_DATABASE_URL", "")
    if "engram_control" not in cp_db:
        fail(f"Control Plane DB URL is not the dedicated control DB: {cp_db}")
    # The core DB name (default engram) must not appear in a CP owner/runtime URL
    # as the database path. The control plane points only at engram_control.
    for key in ("ENGRAM_CONTROL_DATABASE_URL", "ENGRAM_CONTROL_OWNER_DATABASE_URL"):
        url = cp_env.get(key, "")
        if "/engram_control" not in url:
            fail(f"{key} does not target engram_control: {url}")

    # 6. No Core API key reaches Portal.
    for key in portal_env:
        if "API_KEY" in key.upper():
            fail(f"Portal received an API key env var: {key}")

    # 7. Control Plane runs as engram_control_app (runtime role, not owner).
    if "engram_control_app" not in cp_env.get("ENGRAM_CONTROL_DATABASE_URL", ""):
        fail("Control Plane runtime role is not engram_control_app")

    # 8. No NEXT_PUBLIC_* references the internal Control Plane URL.
    for key, value in portal_env.items():
        if key.startswith("NEXT_PUBLIC_") and "control-plane" in str(value):
            fail(f"{key} exposes the internal Control Plane URL to the browser")

    print("OK: hosted compose overlay validated (8 assertions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
