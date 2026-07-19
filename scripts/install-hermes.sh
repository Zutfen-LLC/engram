#!/usr/bin/env bash
# Install Engram into an existing Hermes installation using a provisioned agent key.

set -euo pipefail

readonly INSTALLER_VERSION="0.1.0"
readonly DEFAULT_BASE_URL="https://api.engram.zutfen.com"
readonly DEFAULT_REF="main"
readonly REPOSITORY_URL="https://github.com/Zutfen-LLC/engram.git"
readonly PLUGIN_SUBDIR="adapters/engram-hooks/hermes_plugin/engram_memory"
readonly HERMES_REFERENCE="NousResearch/hermes-agent@36f2a966c7f9f69987494b867c3dcf96b69a5766"

BASE_URL="$DEFAULT_BASE_URL"
PROFILE=""
REF="$DEFAULT_REF"
DRY_RUN=false
TEMP_ROOT=""
PROFILE_CHANGE_STARTED=false
PROFILE_CHANGE_COMMITTED=false
ENV_EXISTED=false
CONFIG_EXISTED=false
ENV_FILE=""
CONFIG_FILE=""
CHECKOUT=""
RESOLVED_COMMIT=""

usage() {
    cat <<'EOF'
Usage: install-hermes.sh [options]

Install Engram dependencies and the engram_memory plugin into the live Hermes
environment using an already-provisioned agent API key.

Options:
  --base-url <url>  Engram service URL (default: https://api.engram.zutfen.com)
  --profile <name>  Target a named Hermes profile
  --ref <git-ref>   Engram branch or tag to install (default: main)
  --dry-run         Show sanitized planned actions without changing anything
  -h, --help        Show this help

Set ENGRAM_API_KEY for non-interactive use. Otherwise the key is read securely
from /dev/tty and never accepted as a command-line argument.
EOF
}

die() {
    printf 'ERROR [%s]: %s\n' "$1" "$2" >&2
    exit 1
}

restore_profile_files() {
    if ! $PROFILE_CHANGE_STARTED || $PROFILE_CHANGE_COMMITTED; then
        return
    fi
    if $ENV_EXISTED; then
        cp -p "$TEMP_ROOT/env.backup" "$ENV_FILE" || true
    else
        rm -f "$ENV_FILE" || true
    fi
    if $CONFIG_EXISTED; then
        cp -p "$TEMP_ROOT/config.backup" "$CONFIG_FILE" || true
    else
        rm -f "$CONFIG_FILE" || true
    fi
    printf 'Profile configuration was restored after the failed stage.\n' >&2
}

cleanup() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        restore_profile_files
    fi
    if [[ -n "$TEMP_ROOT" && -d "$TEMP_ROOT" ]]; then
        rm -rf "$TEMP_ROOT"
    fi
    return "$status"
}
trap cleanup EXIT

require_value() {
    [[ $# -ge 2 && -n "$2" ]] || die "arguments" "$1 requires a value"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --base-url)
            require_value "$@"
            BASE_URL="$2"
            shift 2
            ;;
        --profile)
            require_value "$@"
            PROFILE="$2"
            shift 2
            ;;
        --ref)
            require_value "$@"
            REF="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --api-key|--api-key=*)
            die "arguments" "API keys are not accepted as command-line arguments; use ENGRAM_API_KEY"
            ;;
        *)
            die "arguments" "unknown option: $1"
            ;;
    esac
done

BASE_URL="${BASE_URL%/}"
[[ "$BASE_URL" =~ ^https?://[^[:space:]]+$ ]] || die "arguments" "--base-url must be an HTTP(S) URL"
[[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "$REF" != *".."* ]] \
    || die "arguments" "--ref contains unsupported characters"
if [[ -n "$PROFILE" ]]; then
    [[ "$PROFILE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
        || die "arguments" "--profile contains unsupported characters"
fi

if $DRY_RUN; then
    printf 'Engram Hermes installer dry-run\n'
    printf '  Base URL: %s\n' "$BASE_URL"
    printf '  Profile: %s\n' "${PROFILE:-active/default}"
    printf '  Git ref: %s\n' "$REF"
    printf '  Plan: discover live Hermes paths and interpreter\n'
    printf '  Plan: validate health and the provisioned key (credential omitted)\n'
    printf '  Plan: resolve the requested Git ref once to an immutable commit\n'
    printf '  Plan: install Engram packages and nested engram_memory plugin\n'
    printf '  Plan: require governed automatic capture in the profile environment\n'
    printf '  Plan: atomically update profile environment and configuration\n'
    printf '  No commands, prompts, network requests, installs, or writes were performed.\n'
    exit 0
fi

for command_name in hermes git curl; do
    command -v "$command_name" >/dev/null 2>&1 \
        || die "prerequisites" "required command not found: $command_name"
done

HERMES_EXE=$(command -v hermes)
[[ -x "$HERMES_EXE" ]] || die "Hermes discovery" "resolved hermes executable is not executable"

resolve_python_shebang() {
    local executable=$1 first_line interpreter wrapper_target
    first_line=$(sed -n '1p' "$executable" 2>/dev/null || true)
    [[ "$first_line" == '#!'* ]] || return 1
    interpreter=${first_line#\#!}
    interpreter=${interpreter%%[[:space:]]*}
    if [[ "${interpreter##*/}" == python* && -x "$interpreter" ]]; then
        printf '%s\n' "$interpreter"
        return 0
    fi
    if [[ "$interpreter" == "/usr/bin/env" ]]; then
        local env_name remainder
        remainder=${first_line#\#!/usr/bin/env }
        env_name=${remainder%%[[:space:]]*}
        if [[ "$env_name" == python* ]] && command -v "$env_name" >/dev/null 2>&1; then
            command -v "$env_name"
            return 0
        fi
    fi
    wrapper_target=$(sed -n \
        's/^[[:space:]]*exec[[:space:]]*"\([^"]*\)"[[:space:]]*"\$@".*/\1/p' \
        "$executable" | sed -n '1p')
    if [[ -n "$wrapper_target" && -x "$wrapper_target" ]]; then
        resolve_python_shebang "$wrapper_target"
        return
    fi
    return 1
}

HERMES_PYTHON=$(resolve_python_shebang "$HERMES_EXE" || true)
[[ -n "$HERMES_PYTHON" && -x "$HERMES_PYTHON" ]] \
    || die "Hermes discovery" "could not resolve the Python interpreter used by $HERMES_EXE"
"$HERMES_PYTHON" -c 'import sys; raise SystemExit(0 if sys.executable else 1)' \
    >/dev/null 2>&1 || die "Hermes discovery" "the live Hermes Python interpreter is unusable"

HERMES=(hermes)
if [[ -n "$PROFILE" ]]; then
    HERMES+=(--profile "$PROFILE")
fi

printf '[1/8] Discovering Hermes profile and runtime...\n'
CONFIG_FILE=$("${HERMES[@]}" config path) \
    || die "Hermes discovery" "hermes config path failed"
ENV_FILE=$("${HERMES[@]}" config env-path) \
    || die "Hermes discovery" "hermes config env-path failed"
[[ -n "$CONFIG_FILE" && "$CONFIG_FILE" == /* ]] \
    || die "Hermes discovery" "Hermes returned an invalid config path"
[[ -n "$ENV_FILE" && "$ENV_FILE" == /* ]] \
    || die "Hermes discovery" "Hermes returned an invalid environment path"
[[ -f "$CONFIG_FILE" ]] || die "Hermes discovery" "Hermes config does not exist: $CONFIG_FILE"
[[ "$(dirname "$CONFIG_FILE")" == "$(dirname "$ENV_FILE")" ]] \
    || die "Hermes discovery" "Hermes config and environment paths resolve to different profiles"
printf '  Profile: %s\n' "${PROFILE:-active/default}"
printf '  Python: %s\n' "$HERMES_PYTHON"

TEMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/engram-hermes-installer.XXXXXX") \
    || die "temporary files" "could not create protected temporary directory"
chmod 700 "$TEMP_ROOT"

read_env_key() {
    "$HERMES_PYTHON" - "$ENV_FILE" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)
value = ""
for line in path.read_text(encoding="utf-8").splitlines():
    match = re.match(r"^[ \t]*(?:export[ \t]+)?ENGRAM_API_KEY[ \t]*=(.*)$", line)
    if match:
        candidate = match.group(1).strip()
        if len(candidate) >= 2 and candidate[:1] == candidate[-1:] and candidate[0] in "'\"":
            candidate = candidate[1:-1]
        if candidate:
            value = candidate
print(value, end="")
PY
}

API_KEY="${ENGRAM_API_KEY:-}"
if [[ -z "$API_KEY" ]]; then
    EXISTING_KEY=$(read_env_key) || die "credential" "could not inspect the Hermes environment file"
    if ! { true </dev/tty; } 2>/dev/null; then
        die "credential" "no usable /dev/tty; non-interactive use requires ENGRAM_API_KEY in the process environment"
    fi
    if [[ -n "$EXISTING_KEY" ]]; then
        suffix=${EXISTING_KEY: -4}
        printf 'Keep existing Engram API key ending in ...%s? [Y/n] ' "$suffix" >/dev/tty
        IFS= read -r keep_existing </dev/tty || die "credential" "could not read from /dev/tty"
        case "$keep_existing" in
            ''|y|Y|yes|YES|Yes) API_KEY=$EXISTING_KEY ;;
        esac
    fi
    if [[ -z "$API_KEY" ]]; then
        IFS= read -rsp 'Engram API key: ' API_KEY </dev/tty \
            || die "credential" "could not read API key from /dev/tty"
        printf '\n' >/dev/tty
    fi
fi
unset EXISTING_KEY ENGRAM_API_KEY || true
[[ -n "$API_KEY" ]] || die "credential" "the Engram API key cannot be empty"
[[ "$API_KEY" != *$'\n'* && "$API_KEY" != *$'\r'* ]] \
    || die "credential" "the Engram API key has an invalid format"

curl_escape() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    printf '%s' "$value"
}

request() {
    local endpoint=$1 authenticated=$2 config_file response_file
    config_file=$(mktemp "$TEMP_ROOT/curl.XXXXXX")
    response_file=$(mktemp "$TEMP_ROOT/response.XXXXXX")
    chmod 600 "$config_file" "$response_file"
    {
        printf 'silent\nshow-error\nfail\n'
        printf 'header = "User-Agent: engram-hermes-installer/%s"\n' "$INSTALLER_VERSION"
        if [[ "$authenticated" == true ]]; then
            printf 'header = "Authorization: Bearer %s"\n' "$(curl_escape "$API_KEY")"
        fi
        printf 'url = "%s%s"\n' "$(curl_escape "$BASE_URL")" "$endpoint"
        printf 'output = "%s"\n' "$(curl_escape "$response_file")"
    } >"$config_file"
    chmod 600 "$config_file"
    if ! curl --config "$config_file"; then
        return 1
    fi
    RESPONSE_FILE=$response_file
}

parse_whoami() {
    "$HERMES_PYTHON" - "$RESPONSE_FILE" <<'PY'
import json
import pathlib
import sys

try:
    payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
tenant = str(payload.get("tenant_id") or "")
principal = str(payload.get("principal_id") or payload.get("id") or "")
if not tenant or not principal:
    raise SystemExit(1)
print(f"tenant {tenant[:8]}..., principal {principal[:8]}...")
PY
}

printf '[2/8] Validating Engram connectivity and credential...\n'
request "/health" false || die "health request" "could not reach $BASE_URL/health"
"$HERMES_PYTHON" - "$RESPONSE_FILE" <<'PY' >/dev/null \
    || die "health response" "the service returned an invalid health response"
import json
import pathlib
import sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if str(payload.get("status", "")).lower() not in {"ok", "healthy"}:
    raise SystemExit(1)
PY
request "/whoami" true || die "authentication request" "the provisioned key was rejected or the request failed"
WHOAMI_SUMMARY=$(parse_whoami) || die "authentication response" "the service returned an invalid identity response"
printf '  Authenticated: %s\n' "$WHOAMI_SUMMARY"

printf '[3/8] Resolving the requested Engram ref once...\n'
CHECKOUT="$TEMP_ROOT/engram"
git init --quiet "$CHECKOUT" \
    || die "ref resolution" "could not initialize the temporary Engram checkout"
git -C "$CHECKOUT" fetch --depth 1 "$REPOSITORY_URL" "$REF" \
    || die "ref resolution" "requested Engram ref does not exist or could not be fetched"
RESOLVED_COMMIT=$(git -C "$CHECKOUT" rev-parse --verify 'FETCH_HEAD^{commit}') \
    || die "ref resolution" "requested Engram ref did not resolve to a commit"
[[ "$RESOLVED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] \
    || die "ref resolution" "requested Engram ref resolved to an invalid commit ID"
git -C "$CHECKOUT" checkout --quiet --detach "$RESOLVED_COMMIT" \
    || die "ref resolution" "could not create the detached plugin checkout"
CHECKOUT_COMMIT=$(git -C "$CHECKOUT" rev-parse --verify HEAD) \
    || die "ref resolution" "could not verify the detached plugin checkout"
[[ "$CHECKOUT_COMMIT" == "$RESOLVED_COMMIT" ]] \
    || die "ref resolution" "detached plugin checkout does not match the resolved commit"
printf '  Requested ref: %s\n' "$REF"
printf '  Resolved commit: %s\n' "$RESOLVED_COMMIT"

CLIENT_REF="engram-client @ git+$REPOSITORY_URL@$RESOLVED_COMMIT#subdirectory=sdk/engram-client"
HOOKS_REF="engram-hooks @ git+$REPOSITORY_URL@$RESOLVED_COMMIT#subdirectory=adapters/engram-hooks"
printf '[4/8] Installing Engram packages into the live Hermes environment...\n'
if "$HERMES_PYTHON" -m pip --version >/dev/null 2>&1; then
    "$HERMES_PYTHON" -m pip install --upgrade "$CLIENT_REF" "$HOOKS_REF" \
        || die "Python package install" "pip could not install Engram into the Hermes environment"
elif command -v uv >/dev/null 2>&1; then
    uv pip install --python "$HERMES_PYTHON" --upgrade "$CLIENT_REF" "$HOOKS_REF" \
        || die "Python package install" "uv could not install Engram into the Hermes environment"
else
    die "Python package install" "the Hermes environment lacks pip and uv is unavailable"
fi
"$HERMES_PYTHON" -c 'import engram_client; import engram_hooks' \
    || die "Python import verification" "Engram packages are not importable in the Hermes environment"

printf '[5/8] Installing and enabling the nested Hermes plugin...\n'
if [[ -f "$ENV_FILE" ]]; then
    cp -p "$ENV_FILE" "$TEMP_ROOT/env.backup"
    ENV_EXISTED=true
fi
if [[ -f "$CONFIG_FILE" ]]; then
    cp -p "$CONFIG_FILE" "$TEMP_ROOT/config.backup"
    CONFIG_EXISTED=true
fi
PROFILE_CHANGE_STARTED=true
CHECKOUT_COMMIT=$(git -C "$CHECKOUT" rev-parse --verify HEAD) \
    || die "plugin source checkout" "could not verify the plugin checkout"
[[ "$CHECKOUT_COMMIT" == "$RESOLVED_COMMIT" ]] \
    || die "plugin source checkout" "plugin checkout no longer matches the resolved commit"
PLUGIN_SOURCE="$CHECKOUT/$PLUGIN_SUBDIR"
[[ -f "$PLUGIN_SOURCE/plugin.yaml" ]] \
    || die "plugin source checkout" "the requested ref does not contain the engram_memory plugin"
"${HERMES[@]}" plugins install --force --enable "file://$CHECKOUT#$PLUGIN_SUBDIR" \
    || die "plugin install" "Hermes could not force-install the nested engram_memory plugin"
"${HERMES[@]}" plugins enable engram_memory --no-allow-tool-override \
    || die "plugin enable" "Hermes could not enable engram_memory"

printf '[6/8] Updating the Hermes profile atomically...\n'
"$HERMES_PYTHON" - "$ENV_FILE" \
    3< <(printf '%s\0%s\0%s\0%s\0' "$BASE_URL" "$API_KEY" "true" "true") <<'PY'
import os
import pathlib
import re
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
with os.fdopen(3, "rb") as values_stream:
    raw = values_stream.read().split(b"\0")
if len(raw) < 5:
    raise SystemExit(1)
values = dict(zip(
    (
        "ENGRAM_BASE_URL",
        "ENGRAM_API_KEY",
        "ENGRAM_HOOKS_RECALL_ENABLED",
        "ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE",
    ),
    (part.decode("utf-8") for part in raw[:4]),
    strict=True,
))
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
keys = set(values)
kept = []
for line in lines:
    match = re.match(r"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", line)
    if not match or match.group(1) not in keys:
        kept.append(line)
if kept and kept[-1] != "":
    kept.append("")
kept.extend(f"{key}={values[key]}" for key in values)
content = "\n".join(kept) + "\n"
path.parent.mkdir(parents=True, exist_ok=True)
fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp_name, path)
    os.chmod(path, 0o600)
except BaseException:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(temp_name)
    except OSError:
        pass
    raise
PY
"${HERMES[@]}" config set memory.memory_enabled true \
    || die "Hermes configuration" "could not enable memory"
"${HERMES[@]}" config set memory.provider engram_memory \
    || die "Hermes configuration" "could not select the engram_memory provider"

printf '[7/8] Verifying installation...\n'
"$HERMES_PYTHON" - "$ENV_FILE" <<'PY' \
    || die "environment verification" "required Engram entries are missing or duplicated"
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
if stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise SystemExit(1)
text = path.read_text(encoding="utf-8")
for key in (
    "ENGRAM_BASE_URL",
    "ENGRAM_API_KEY",
    "ENGRAM_HOOKS_RECALL_ENABLED",
    "ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE",
):
    matches = re.findall(rf"(?m)^[ \t]*(?:export[ \t]+)?{key}[ \t]*=", text)
    if len(matches) != 1:
        raise SystemExit(1)
PY
"$HERMES_PYTHON" -c 'import engram_client; import engram_hooks' \
    || die "Python import verification" "Engram packages stopped importing"
"$HERMES_PYTHON" -c '
from agent.memory_provider import MemoryProvider
from tools.memory_tool import memory_tool
assert isinstance(MemoryProvider, type) and callable(memory_tool)
' || die "Hermes compatibility verification" \
    "required target tools.memory_tool.memory_tool is unavailable in the live Hermes runtime; inspected contract: $HERMES_REFERENCE; reinstall a compatible Engram plugin or use deliberate recall-only mode"
"$HERMES_PYTHON" - verify-direct-url "$RESOLVED_COMMIT" <<'PY' \
    || die "Python revision verification" "installed Engram packages do not match the resolved commit"
import importlib.metadata
import json
import sys

expected = sys.argv[2]
for distribution_name in ("engram-client", "engram-hooks"):
    distribution = importlib.metadata.distribution(distribution_name)
    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text is None:
        raise SystemExit(1)
    direct_url = json.loads(direct_url_text)
    commit = str(direct_url.get("vcs_info", {}).get("commit_id", ""))
    if commit != expected:
        raise SystemExit(1)
PY
PLUGIN_LIST=$("${HERMES[@]}" plugins list --plain --no-bundled) \
    || die "plugin verification" "Hermes could not list installed plugins"
printf '%s\n' "$PLUGIN_LIST" | "$HERMES_PYTHON" -c '
import re, sys
text = sys.stdin.read()
line = next((line for line in text.splitlines() if "engram_memory" in line), "")
match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", line)
if not line or "enabled" not in line.lower() or not match:
    raise SystemExit(1)
if tuple(map(int, match.groups())) < (0, 2, 0):
    raise SystemExit(1)
' || die "plugin verification" "engram_memory is missing, disabled, or older than 0.2.0"
[[ "$("${HERMES[@]}" config get memory.memory_enabled)" == "true" ]] \
    || die "configuration verification" "Hermes memory is not enabled"
[[ "$("${HERMES[@]}" config get memory.provider)" == "engram_memory" ]] \
    || die "configuration verification" "the active memory provider is not engram_memory"
request "/whoami" true || die "final authentication" "final credential validation failed"
parse_whoami >/dev/null || die "final authentication" "final identity response was invalid"

PROFILE_CHANGE_COMMITTED=true
unset API_KEY

printf '[8/8] Running Hermes diagnostics...\n'
set +e
"${HERMES[@]}" doctor >/dev/null 2>&1
DOCTOR_STATUS=$?
set -e
if [[ $DOCTOR_STATUS -ne 0 ]]; then
    printf '  Warning: hermes doctor reported pre-existing issues; Engram verification passed.\n'
else
    printf '  Hermes diagnostics completed.\n'
fi

printf '\nEngram package, profile, and stock-Hermes API-shape checks are verified.\n'
printf 'Automatic write interception activates inside Hermes after restart and is configured fail-loud.\n'
printf 'Installed revision: %s -> %s\n' "$REF" "$RESOLVED_COMMIT"
printf 'Interactive CLI: fully exit and relaunch Hermes.\n'
printf 'Installed gateway: hermes gateway restart\n'
printf 'Post-restart check: confirm logs contain "stock-Hermes interception active: tools.memory_tool.memory_tool".\n'
