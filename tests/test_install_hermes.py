"""Behavioral tests for the standalone Hermes installer."""

from __future__ import annotations

import os
import pty
import secrets
import select
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install-hermes.sh"
_USE_HARNESS_KEY = object()


@dataclass
class Harness:
    root: Path
    bin_dir: Path
    profile: Path
    log: Path
    env: dict[str, str]
    key: str
    resolved_sha: str
    advanced_sha: str

    def run(
        self,
        *args: str,
        key: str | None | object = _USE_HARNESS_KEY,
        stdin_script: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if key is _USE_HARNESS_KEY:
            key = self.key
        if key is None:
            env.pop("ENGRAM_API_KEY", None)
        else:
            env["ENGRAM_API_KEY"] = key
        command = ["bash"]
        input_text: str | None = None
        if stdin_script:
            command.extend(["-s", "--", *args])
            input_text = INSTALLER.read_text(encoding="utf-8")
        else:
            command.extend([str(INSTALLER), *args])
        return subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    bin_dir = tmp_path / "bin"
    live_bin = tmp_path / "hermes-live" / "bin"
    profile = tmp_path / "profile"
    bin_dir.mkdir()
    live_bin.mkdir(parents=True)
    profile.mkdir()
    config = profile / "config.yaml"
    config.write_text(
        "name: test\nmemory:\n  provider: builtin\nplugins:\n  enabled:\n    - calculator\n",
        encoding="utf-8",
    )
    env_file = profile / ".env"
    env_file.write_text(
        "# keep this comment\nUNRELATED=value\nENGRAM_BASE_URL=old\n"
        "ENGRAM_BASE_URL=duplicate\nENGRAM_HOOKS_RECALL_ENABLED=false\n",
        encoding="utf-8",
    )
    env_file.chmod(0o640)
    log = tmp_path / "commands.log"
    resolved_sha = "1" * 40
    advanced_sha = "2" * 40

    live_python = live_bin / "python3"
    _write_executable(
        live_python,
        f"""#!/usr/bin/env bash
set -eu
printf 'python:%s\\n' "$*" >>"$INSTALLER_TEST_LOG"
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "pip" ]]; then
  [[ "${{INSTALLER_TEST_NO_PIP:-0}}" != 1 ]] || exit 1
  exit 0
fi
if [[ "${{1:-}}" == "-c" && "${{2:-}}" == *"import engram_client"* ]]; then
  exit 0
fi
if [[ "${{1:-}}" == "-c" && "${{2:-}}" == *"tools.memory_tool"* ]]; then
  [[ "${{INSTALLER_TEST_HERMES_API_DRIFT:-0}}" != 1 ]] || exit 1
  exit 0
fi
if [[ "${{1:-}}" == "-" && "${{2:-}}" == "verify-direct-url" ]]; then
  [[ "${{3:-}}" == "$INSTALLER_TEST_RESOLVED_SHA" ]]
  exit
fi
exec {sys.executable!s} "$@"
""",
    )

    live_hermes = live_bin / "hermes"
    live_hermes.write_text(
        f"#!{live_python}\n# fake console script; wrapper handles behavior\n",
        encoding="utf-8",
    )
    live_hermes.chmod(0o755)

    _write_executable(
        bin_dir / "hermes",
        f"""#!/usr/bin/env bash
set -eu
if false; then
  exec "{live_hermes}" "$@"
fi
printf 'hermes:%s\\n' "$*" >>"$INSTALLER_TEST_LOG"
args=("$@")
if [[ "${{args[0]:-}}" == "--profile" ]]; then args=("${{args[@]:2}}"); fi
case "${{args[*]:-}}" in
  'config path') printf '%s\\n' "$INSTALLER_TEST_CONFIG" ;;
  'config env-path') printf '%s\\n' "$INSTALLER_TEST_ENV" ;;
  'config set memory.memory_enabled true')
    printf '\\n# installer-memory-enabled\\n' >>"$INSTALLER_TEST_CONFIG" ;;
  'config set memory.provider engram_memory')
    if [[ "${{INSTALLER_TEST_CONFIG_FAIL:-0}}" == 1 ]]; then
      printf '\\n# partial-mutation\\n' >>"$INSTALLER_TEST_CONFIG"
      exit 42
    fi
    printf '# installer-provider\\n' >>"$INSTALLER_TEST_CONFIG" ;;
  'config get memory.memory_enabled') printf 'true\\n' ;;
  'config get memory.provider') printf 'engram_memory\\n' ;;
  'plugins install '*) exit 0 ;;
  *'plugins enable '*'--no-allow-tool-override'*) exit 0 ;;
  'plugins list --plain --no-bundled')
    printf 'engram_memory  0.2.0  enabled\\ncalculator  1.0.0  enabled\\n' ;;
  'doctor')
    printf 'pre-existing provider warning\\n'
    exit "${{INSTALLER_TEST_DOCTOR_STATUS:-0}}" ;;
  *) printf 'unexpected hermes invocation: %s\\n' "${{args[*]:-}}" >&2; exit 64 ;;
esac
""",
    )

    _write_executable(
        bin_dir / "curl",
        r"""#!/usr/bin/env bash
set -eu
printf 'curl:%s\\n' "$*" >>"$INSTALLER_TEST_LOG"
cfg=''
while [[ $# -gt 0 ]]; do
  if [[ "$1" == '--config' ]]; then cfg="$2"; shift 2; else shift; fi
done
[[ -n "$cfg" && -r "$cfg" ]] || exit 91
mode=$(stat -c '%a' "$cfg" 2>/dev/null || stat -f '%Lp' "$cfg")
[[ "$mode" == 600 ]] || exit 92
grep -q 'User-Agent: engram-hermes-installer/' "$cfg" || exit 93
url=$(sed -n 's/^url = "\(.*\)"$/\1/p' "$cfg")
output=$(sed -n 's/^output = "\(.*\)"$/\1/p' "$cfg")
case "$url" in
  */health)
    [[ "${INSTALLER_TEST_HEALTH_FAIL:-0}" != 1 ]] || exit 22
    printf '{"status":"ok"}\n' >"$output" ;;
  */whoami)
    grep -q 'Authorization: Bearer ' "$cfg" || exit 94
    [[ "${INSTALLER_TEST_AUTH_FAIL:-0}" != 1 ]] || exit 22
    printf '{"tenant_id":"tenant-123456789","principal_id":"principal-987654321"}\n' >"$output" ;;
  *) exit 95 ;;
esac
""",
    )

    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
set -eu
printf 'git:%s\\n' "$*" >>"$INSTALLER_TEST_LOG"
if [[ "$*" == *"$INSTALLER_TEST_MISSING_REF"* && -n "$INSTALLER_TEST_MISSING_REF" ]]; then
  exit 2
fi
if [[ "${1:-}" == "init" ]]; then
  mkdir -p "${!#}"
  exit 0
fi
if [[ "${1:-}" != "-C" ]]; then
  exit 64
fi
repo=$2
shift 2
case "${1:-}" in
  fetch)
    count_file="$INSTALLER_TEST_GIT_FETCH_COUNT"
    count=0
    [[ ! -f "$count_file" ]] || count=$(cat "$count_file")
    count=$((count + 1))
    printf '%s' "$count" >"$count_file"
    sha="$INSTALLER_TEST_RESOLVED_SHA"
    if [[ "${INSTALLER_TEST_ADVANCE_BRANCH:-0}" == 1 && "$count" -gt 1 ]]; then
      sha="$INSTALLER_TEST_ADVANCED_SHA"
    fi
    printf '%s' "$sha" >"$repo/.fetch-sha"
    ;;
  rev-parse)
    if [[ "${*: -1}" == 'FETCH_HEAD^{commit}' ]]; then
      cat "$repo/.fetch-sha"
    else
      cat "$repo/.head-sha"
    fi
    ;;
  checkout)
    sha="${!#}"
    printf '%s' "$sha" >"$repo/.head-sha"
    plugin_dir="$repo/adapters/engram-hooks/hermes_plugin/engram_memory"
    mkdir -p "$plugin_dir"
    printf 'name: engram_memory\nversion: 0.2.0\n' >"$plugin_dir/plugin.yaml"
    printf '# plugin\n' >"$plugin_dir/__init__.py"
    ;;
  *) exit 65 ;;
esac
""",
    )

    _write_executable(
        bin_dir / "uv",
        """#!/usr/bin/env bash
set -eu
printf 'uv:%s\\n' "$*" >>"$INSTALLER_TEST_LOG"
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "INSTALLER_TEST_LOG": str(log),
        "INSTALLER_TEST_CONFIG": str(config),
        "INSTALLER_TEST_ENV": str(env_file),
        "INSTALLER_TEST_RESOLVED_SHA": resolved_sha,
        "INSTALLER_TEST_ADVANCED_SHA": advanced_sha,
        "INSTALLER_TEST_GIT_FETCH_COUNT": str(tmp_path / "git-fetch-count"),
        "INSTALLER_TEST_MISSING_REF": "never-matches",
    }
    return Harness(
        tmp_path,
        bin_dir,
        profile,
        log,
        env,
        f"eng_test_{secrets.token_hex(16)}",
        resolved_sha,
        advanced_sha,
    )


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_help_lists_supported_options_without_external_calls(harness: Harness) -> None:
    result = harness.run("--help", key=None)
    assert result.returncode == 0
    assert "https://api.engram.zutfen.com" in result.stdout
    assert "--base-url" in result.stdout
    assert "--profile" in result.stdout
    assert "--ref" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--api-key" not in result.stdout
    assert not harness.log.exists()


def test_dry_run_has_no_side_effects_or_secret_prompt(harness: Harness) -> None:
    before = {path: path.read_bytes() for path in harness.profile.iterdir()}
    result = harness.run("--dry-run", "--profile", "work", "--ref", "v1.2.3", key=None)
    assert result.returncode == 0
    assert "dry-run" in result.stdout.lower()
    assert "Base URL: https://api.engram.zutfen.com" in result.stdout
    assert {path: path.read_bytes() for path in harness.profile.iterdir()} == before
    assert not harness.log.exists()


def test_missing_tty_without_key_fails_before_modification(harness: Harness) -> None:
    before = {path: path.read_bytes() for path in harness.profile.iterdir()}
    result = harness.run(key=None)
    assert result.returncode != 0
    assert "/dev/tty" in _combined(result)
    assert "ENGRAM_API_KEY" in _combined(result)
    assert {path: path.read_bytes() for path in harness.profile.iterdir()} == before


@pytest.mark.parametrize("failure_var", ["INSTALLER_TEST_HEALTH_FAIL", "INSTALLER_TEST_AUTH_FAIL"])
def test_http_failure_makes_no_profile_changes_and_never_leaks_key(
    harness: Harness, failure_var: str
) -> None:
    harness.env[failure_var] = "1"
    before = {path: path.read_bytes() for path in harness.profile.iterdir()}
    result = harness.run()
    assert result.returncode != 0
    assert {path: path.read_bytes() for path in harness.profile.iterdir()} == before
    assert harness.key not in _combined(result)
    assert "plugins install" not in (harness.log.read_text() if harness.log.exists() else "")


def test_symbolic_ref_is_resolved_once_and_used_for_all_artifacts(harness: Harness) -> None:
    result = harness.run("--profile", "work", "--ref", "release/0.2", stdin_script=True)
    assert result.returncode == 0, _combined(result)
    output = _combined(result)
    log = harness.log.read_text()
    assert harness.key not in output
    assert harness.key not in log
    assert "hermes:--profile work config path" in log
    assert "python:-m pip install --upgrade" in log
    assert f"engram.git@{harness.resolved_sha}#subdirectory=sdk/engram-client" in log
    assert f"engram.git@{harness.resolved_sha}#subdirectory=adapters/engram-hooks" in log
    assert log.count("fetch --depth 1") == 1
    assert log.count("release/0.2") == 1
    assert f"checkout --quiet --detach {harness.resolved_sha}" in log
    assert f"python:- verify-direct-url {harness.resolved_sha}" in log
    assert "adapters/engram-hooks/hermes_plugin/engram_memory" in log
    assert "plugins install --force --enable file://" in log
    assert "plugins enable" in log
    assert "--no-allow-tool-override" in log
    assert log.count("curl:") == 3
    assert "Requested ref: release/0.2" in output
    assert f"Resolved commit: {harness.resolved_sha}" in output
    assert "restart" in output.lower()


def test_branch_advancing_after_resolution_cannot_mix_artifacts(harness: Harness) -> None:
    harness.env["INSTALLER_TEST_ADVANCE_BRANCH"] = "1"
    result = harness.run("--ref", "moving-branch")
    assert result.returncode == 0, _combined(result)
    log = harness.log.read_text()
    assert log.count("moving-branch") == 1
    assert log.count(f"engram.git@{harness.resolved_sha}#subdirectory=") == 2
    assert f"checkout --quiet --detach {harness.resolved_sha}" in log
    assert harness.advanced_sha not in log


def test_nonexistent_ref_fails_before_packages_or_profile_mutation(harness: Harness) -> None:
    missing_ref = "missing-release"
    harness.env["INSTALLER_TEST_MISSING_REF"] = missing_ref
    before = {path: path.read_bytes() for path in harness.profile.iterdir()}
    result = harness.run("--ref", missing_ref)
    assert result.returncode != 0
    assert "ref resolution" in _combined(result).lower()
    assert {path: path.read_bytes() for path in harness.profile.iterdir()} == before
    log = harness.log.read_text()
    assert "python:-m pip install" not in log
    assert "plugins install" not in log
    assert "config set" not in log


def test_env_update_is_atomic_secure_preserving_and_idempotent(harness: Harness) -> None:
    first = harness.run()
    assert first.returncode == 0, _combined(first)
    second = harness.run()
    assert second.returncode == 0, _combined(second)
    env_file = harness.profile / ".env"
    content = env_file.read_text()
    assert "# keep this comment" in content
    assert "UNRELATED=value" in content
    assert content.count("ENGRAM_BASE_URL=") == 1
    assert content.count("ENGRAM_API_KEY=") == 1
    assert content.count("ENGRAM_HOOKS_RECALL_ENABLED=") == 1
    assert content.count("ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE=") == 1
    assert "ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE=true" in content
    assert content.count(harness.key) == 1
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert harness.key not in _combined(first) + _combined(second)


def test_config_failure_restores_profile_files(harness: Harness) -> None:
    config = harness.profile / "config.yaml"
    env_file = harness.profile / ".env"
    config_before = config.read_bytes()
    env_before = env_file.read_bytes()
    mode_before = stat.S_IMODE(env_file.stat().st_mode)
    harness.env["INSTALLER_TEST_CONFIG_FAIL"] = "1"
    result = harness.run()
    assert result.returncode != 0
    assert config.read_bytes() == config_before
    assert env_file.read_bytes() == env_before
    assert stat.S_IMODE(env_file.stat().st_mode) == mode_before
    assert harness.key not in _combined(result)


def test_hermes_api_drift_rolls_back_profile_and_fails_loudly(harness: Harness) -> None:
    config = harness.profile / "config.yaml"
    env_file = harness.profile / ".env"
    config_before = config.read_bytes()
    env_before = env_file.read_bytes()
    harness.env["INSTALLER_TEST_HERMES_API_DRIFT"] = "1"

    result = harness.run()

    assert result.returncode != 0
    assert "tools.memory_tool.memory_tool" in _combined(result)
    assert "36f2a966c7f9f69987494b867c3dcf96b69a5766" in _combined(result)
    assert config.read_bytes() == config_before
    assert env_file.read_bytes() == env_before
    assert harness.key not in _combined(result)


def test_pip_falls_back_to_uv_for_the_same_interpreter(harness: Harness) -> None:
    harness.env["INSTALLER_TEST_NO_PIP"] = "1"
    result = harness.run()
    assert result.returncode == 0, _combined(result)
    log = harness.log.read_text()
    assert "uv:pip install --python" in log
    assert str(harness.root / "hermes-live" / "bin" / "python3") in log


def test_doctor_warnings_are_nonfatal(harness: Harness) -> None:
    harness.env["INSTALLER_TEST_DOCTOR_STATUS"] = "1"
    result = harness.run()
    assert result.returncode == 0, _combined(result)
    assert "warning" in _combined(result).lower()


@pytest.mark.skipif(not hasattr(pty, "fork"), reason="requires a POSIX pseudo-terminal")
def test_piped_script_can_retain_existing_key_through_dev_tty(harness: Harness) -> None:
    existing_key = f"eng_existing_{secrets.token_hex(16)}"
    suffix = existing_key[-4:]
    env_file = harness.profile / ".env"
    env_file.write_text(env_file.read_text() + f"ENGRAM_API_KEY={existing_key}\n")
    env = dict(harness.env)
    env.pop("ENGRAM_API_KEY", None)
    read_fd, write_fd = os.pipe()
    pid, master_fd = pty.fork()
    if pid == 0:  # pragma: no cover - assertions happen in the parent
        os.close(write_fd)
        os.dup2(read_fd, 0)
        os.close(read_fd)
        os.execve("/bin/bash", ["bash", "-s"], env)

    os.close(read_fd)
    os.write(write_fd, INSTALLER.read_bytes())
    os.close(write_fd)
    output = bytearray()
    answered = False
    done = 0
    status = 0
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            prompt = f"Keep existing Engram API key ending in ...{suffix}?".encode()
            if prompt in output and not answered:
                os.write(master_fd, b"\n")
                answered = True
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            break
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        pytest.fail("pseudo-terminal installer smoke test timed out")
    os.close(master_fd)
    if not done:
        _, status = os.waitpid(pid, 0)
    rendered = output.decode(errors="replace")
    assert answered
    assert os.waitstatus_to_exitcode(status) == 0, rendered
    assert existing_key not in rendered
    assert env_file.read_text().count("ENGRAM_API_KEY=") == 1
