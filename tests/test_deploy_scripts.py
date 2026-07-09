"""Smoke tests for deployment shell scripts.

Runs ``bash -n`` (syntax check) and ``shellcheck`` (when available) against the
shipped scripts so a broken backup script is caught before it reaches an
operator. ``shellcheck`` is optional — the test skips cleanly if it is not
installed, matching the acceptance criterion's "where available".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    REPO_ROOT / "deploy" / "backup.sh",
    REPO_ROOT / "migrations" / "app_role_password.sh",
]


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_exists(script: Path):
    assert script.is_file(), f"missing script: {script}"


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_is_executable(script: Path):
    # Executable bit should be set so operators can run ./deploy/backup.sh.
    assert script.stat().st_mode & 0o100, f"{script} is not executable"


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_bash_syntax(script: Path):
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"bash -n failed for {script}:\n{result.stderr}"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_shellcheck(script: Path):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    result = subprocess.run(
        ["shellcheck", str(script)], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"shellcheck failed for {script}:\n{result.stdout}{result.stderr}"
    )
