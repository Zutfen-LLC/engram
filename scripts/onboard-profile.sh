#!/usr/bin/env bash
#
# Engram Hermes Profile Onboarding
#
# Creates an agent principal + scoped API key via the self-service /v1/agents
# endpoint and wires a Hermes profile to use the engram_memory MemoryProvider
# plugin.
#
# Usage:
#   ./scripts/onboard-profile.sh --profile <name> [options]
#
# Required:
#   --profile <name>          Hermes profile name (e.g. "myagent")
#
# Options:
#   --base-url <url>          Engram service URL (default: https://engram.zutfen.com)
#   --user-key <key>          User-level API key with write scope (creates agents).
#                             Auto-read from engram profile .env if omitted.
#   --principal-name <name>   Agent principal name (default: <profile>-agent)
#   --scopes <csv>            API key scopes for the new agent (default: read,write)
#   --dry-run                  Show actions without executing
#
# The user key needs only read+write scope — no admin access required.
# The script uses POST /v1/agents (self-service, tenant-scoped, RLS-enforced).

set -euo pipefail

# --- Defaults ---------------------------------------------------------------

BASE_URL="${ENGRAM_BASE_URL:-https://engram.zutfen.com}"
USER_KEY="${ENGRAM_API_KEY:-}"
PROFILE_NAME=""
PRINCIPAL_NAME=""
DRY_RUN=false
SCOPES="read,write"

# HERMES_HOME may be set by the active Hermes session to the profile dir
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
if [[ ! -d "$HERMES_HOME/profiles" ]]; then
    HERMES_HOME="$HOME/.hermes"
fi
ENGRAM_HOOKS_VENV="${HERMES_VENV:-$HOME/.hermes/hermes-agent/venv}"

# --- Arg parsing ------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)        PROFILE_NAME="$2"; shift 2 ;;
        --base-url)       BASE_URL="$2"; shift 2 ;;
        --user-key)       USER_KEY="$2"; shift 2 ;;
        --principal-name) PRINCIPAL_NAME="$2"; shift 2 ;;
        --scopes)         SCOPES="$2"; shift 2 ;;
        --hermes-home)    HERMES_HOME="$2"; shift 2 ;;
        --venv)           ENGRAM_HOOKS_VENV="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=true; shift ;;
        -h|--help)        grep '^#' "$0" | head -30; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# --- Validation -------------------------------------------------------------

[[ -z "$PROFILE_NAME" ]] && { echo "ERROR: --profile is required"; exit 1; }

# Auto-read user key from the engram profile .env if not provided
if [[ -z "$USER_KEY" ]]; then
    ENV_FILE="$HERMES_HOME/profiles/engram/.env"
    if [[ -f "$ENV_FILE" ]]; then
        USER_KEY=$(grep ENGRAM_API_KEY "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)
    fi
fi
[[ -z "$USER_KEY" ]] && { echo "ERROR: No API key. Use --user-key or set ENGRAM_API_KEY"; exit 1; }

[[ -z "$PRINCIPAL_NAME" ]] && PRINCIPAL_NAME="${PROFILE_NAME}-agent"

PROFILE_DIR="$HERMES_HOME/profiles/$PROFILE_NAME"
[[ ! -d "$PROFILE_DIR" ]] && { echo "ERROR: Profile dir not found: $PROFILE_DIR"; exit 1; }

PLUGIN_DIR="$HERMES_HOME/plugins/engram_memory"
REPO_PLUGIN="$HOME/code/engram/adapters/engram-hooks/hermes_plugin/engram_memory"

echo "Engram Profile Onboarding"
echo "=========================="
echo "  Profile:       $PROFILE_NAME"
echo "  Engram URL:    $BASE_URL"
echo "  Principal:     $PRINCIPAL_NAME"
echo "  Scopes:        $SCOPES"
echo "  Dry run:       $DRY_RUN"
echo ""

# --- Step 1: Verify connectivity --------------------------------------------

echo "[1/3] Verifying Engram connectivity..."

HEALTH=$(curl -sf "$BASE_URL/health" 2>/dev/null || echo "")
if [[ "$HEALTH" != *"ok"* ]]; then
    echo "  ERROR: Engram service not reachable at $BASE_URL"
    exit 1
fi
echo "  Health: OK"

WHOAMI_TENANT=$(curl -sf "$BASE_URL/whoami" \
    -H "Authorization: Bearer $USER_KEY" \
    2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tenant_id', ''))
except Exception:
    pass
" 2>/dev/null || echo "")

if [[ -z "$WHOAMI_TENANT" ]]; then
    echo "  ERROR: Could not authenticate. Check that the API key is valid."
    exit 1
fi
echo "  Authenticated as tenant: ${WHOAMI_TENANT:0:8}..."

# --- Step 2: Create agent via self-service endpoint -------------------------

echo ""
echo "[2/3] Creating agent '$PRINCIPAL_NAME'..."

SCOPES_JSON=$(python3 -c "print('[' + ','.join(repr(s.strip()) for s in '$SCOPES'.split(',')) + ']')")
AGENT_PAYLOAD="{\"name\": \"$PRINCIPAL_NAME\", \"scopes\": $SCOPES_JSON, \"label\": \"$PROFILE_NAME\"}"

if $DRY_RUN; then
    echo "  [dry-run] Would create agent: $AGENT_PAYLOAD"
    NEW_API_KEY="eng_dryrun_placeholder"
    AGENT_ID="dry-run"
else
    RESPONSE=$(curl -sf "$BASE_URL/v1/agents" \
        -H "Authorization: Bearer $USER_KEY" \
        -H "Content-Type: application/json" \
        -d "$AGENT_PAYLOAD" 2>/dev/null || echo "")

    if [[ -z "$RESPONSE" ]]; then
        echo "  ERROR: Failed to create agent"
        exit 1
    fi

    NEW_API_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null || echo "")
    AGENT_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")

    if [[ -z "$NEW_API_KEY" || "$NEW_API_KEY" == "None" ]]; then
        echo "  ERROR: Agent created but no key returned"
        exit 1
    fi
    echo "  Agent ID: $AGENT_ID"
    echo "  API key:  ${NEW_API_KEY:0:12}...${NEW_API_KEY: -4}"
fi

# --- Step 3: Install plugin + config + hooks --------------------------------

echo ""
echo "[3/3] Installing plugin + config..."

if $DRY_RUN; then
    echo "  [dry-run] Would copy plugin + write .env + update config.yaml + check hooks"
else
    # Plugin
    if [[ -d "$REPO_PLUGIN" ]]; then
        mkdir -p "$PLUGIN_DIR"
        cp -r "$REPO_PLUGIN"/* "$PLUGIN_DIR/"
        echo "  Plugin copied from repo"
    elif [[ -d "$PLUGIN_DIR" ]]; then
        echo "  Plugin already installed"
    else
        echo "  WARNING: Plugin source not found. Copy manually from engram repo:"
        echo "           cp -r adapters/engram-hooks/hermes_plugin/engram_memory/* ~/.hermes/plugins/engram_memory/"
    fi

    # .env — write agent key (NOT the user key)
    ENV_FILE="$PROFILE_DIR/.env"
    touch "$ENV_FILE"
    grep -q "^ENGRAM_BASE_URL=" "$ENV_FILE" && sed -i "s|^ENGRAM_BASE_URL=.*|ENGRAM_BASE_URL=$BASE_URL|" "$ENV_FILE" || echo "ENGRAM_BASE_URL=$BASE_URL" >> "$ENV_FILE"
    grep -q "^ENGRAM_API_KEY=" "$ENV_FILE" && sed -i "s|^ENGRAM_API_KEY=.*|ENGRAM_API_KEY=$NEW_API_KEY|" "$ENV_FILE" || echo "ENGRAM_API_KEY=$NEW_API_KEY" >> "$ENV_FILE"
    echo "  .env updated with agent key"

    # config.yaml — set provider
    CONFIG_FILE="$PROFILE_DIR/config.yaml"
    if [[ -f "$CONFIG_FILE" ]]; then
        if grep -q "^memory:" "$CONFIG_FILE"; then
            if grep -A 10 "^memory:" "$CONFIG_FILE" | grep -q "provider:"; then
                sed -i "/^memory:/,/^[^ ]/ s/provider:.*/provider: engram_memory/" "$CONFIG_FILE"
            else
                sed -i "/^memory:/a\\  provider: engram_memory" "$CONFIG_FILE"
            fi
        else
            echo -e "\nmemory:\n  memory_enabled: true\n  user_profile_enabled: true\n  provider: engram_memory" >> "$CONFIG_FILE"
        fi

        if ! grep -q "ENGRAM_HOOKS_COMPAT_SHIM" "$CONFIG_FILE"; then
            sed -i "/^memory:/a\\  ENGRAM_HOOKS_COMPAT_SHIM: true\n  ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE: false" "$CONFIG_FILE"
        fi
        echo "  config.yaml updated"
    else
        echo "  WARNING: config.yaml not found at $CONFIG_FILE"
    fi

    # engram-hooks in venv
    if [[ -d "$ENGRAM_HOOKS_VENV" ]]; then
        if "$ENGRAM_HOOKS_VENV/bin/pip" show engram-hooks &>/dev/null; then
            echo "  engram-hooks already installed"
        else
            "$ENGRAM_HOOKS_VENV/bin/pip" install engram-hooks 2>&1 | tail -1
            echo "  engram-hooks installed"
        fi
    else
        echo "  WARNING: Venv not found at $ENGRAM_HOOKS_VENV"
    fi
fi

# --- Done -------------------------------------------------------------------

echo ""
echo "=========================="
echo "Onboarding complete!"
echo ""
echo "Next steps:"
echo "  1. Restart the profile: hermes -p $PROFILE_NAME"
echo "  2. Check startup log for: 'Memory provider engram_memory registered'"
echo "  3. Write a test memory and verify it in Engram"
echo ""

if ! $DRY_RUN; then
    echo "IMPORTANT: The agent API key was written to $PROFILE_DIR/.env"
    echo "          It is shown only once above. Store it safely."
fi
