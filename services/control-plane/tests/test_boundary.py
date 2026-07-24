"""Hard boundary assertion: the Control Plane never imports Engram Core.

This is a load-bearing invariant (ENG-PORTAL-001A): any future import of the
``engram`` package from within ``engram_control_plane`` breaks the service
boundary and must fail this test.
"""

from __future__ import annotations

import importlib
import pkgutil


def _walk_package(pkg_name: str) -> list[str]:
    pkg = importlib.import_module(pkg_name)
    modules: list[str] = [pkg_name]
    if not hasattr(pkg, "__path__"):
        return modules
    for _finder, name, _ispkg in pkgutil.walk_packages(pkg.__path__, prefix=f"{pkg_name}."):
        modules.append(name)
    return modules


def test_control_plane_does_not_import_core() -> None:
    """No Control Plane submodule imports the ``engram`` Core package."""
    import sys

    # Ensure a clean slate of engram_control_plane imports.
    for mod in list(sys.modules):
        if mod.startswith("engram_control_plane"):
            del sys.modules[mod]
    # Remove any accidental core import too.
    core_loaded_before = any(m == "engram" or m.startswith("engram.") for m in sys.modules)

    modules = _walk_package("engram_control_plane")
    assert modules, "expected to discover engram_control_plane submodules"
    # env.py is an Alembic runtime script meant to execute under `alembic`, not
    # a normally-importable module; importing it standalone raises. The textual
    # scan below still covers it.
    skip = {"engram_control_plane.migrations.env"}
    for name in modules:
        if name in skip:
            continue
        importlib.import_module(name)

    newly_loaded_core = {
        m for m in sys.modules if (m == "engram" or m.startswith("engram."))
        and not (m == "engram_control_plane" or m.startswith("engram_control_plane."))
    }
    # If core was already loaded by the test harness (e.g. shared env), we can
    # only assert the control plane's source files don't reference it textually.
    import pathlib

    pkg_root = pathlib.Path(importlib.import_module("engram_control_plane").__file__).parent
    offenders: list[str] = []
    for py in pkg_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # Allow mentions in comments/docstrings only is too brittle; require the
        # package not import 'engram' or 'from engram'. The package's own name
        # starts with 'engram_control_plane', which is allowed.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Detect imports of the core package specifically.
            if "import engram" in stripped and "engram_control_plane" not in stripped:
                offenders.append(f"{py}: {stripped}")
            if (
                stripped.startswith("from engram ") or stripped.startswith("from engram.")
            ) and not stripped.startswith("from engram_control_plane"):
                offenders.append(f"{py}: {stripped}")
    assert not offenders, (
        "Control Plane must not import Engram Core. Offenders:\n" + "\n".join(offenders)
    )

    # If core wasn't loaded before this test, it must not be loaded now.
    if not core_loaded_before:
        assert not newly_loaded_core, f"Control Plane pulled in Core: {newly_loaded_core}"
