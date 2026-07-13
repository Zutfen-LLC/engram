#!/usr/bin/env bash
#
# Engram Hermes Profile Onboarding
#
# Creates a principal + scoped API key in Engram and wires a Hermes profile
# to use the engram_memory MemoryProvider plugin.
#
# Usage:
#   ./scripts/onboard-profile.sh --profile <name> [options]
#
# Required:
#   --profile <name>          Hermes profile name (e.g. "myagent")
#
# Options:
#   --base-url <url>          Public Engram URL for the profile (default: https://engram.zutfen.com)
#   --admin-url <url>         URL for admin API (LAN-only if rproxy blocks /v1/admin/)
#   --admin-key <key>         Admin API key (auto-read from engram profile .env if omitted)
#   --tenant-id <uuid>        Engram tenant ID (auto-discovered if omitted)
#   --principal-name <name>   Principal name (default: <profile>-agent)
#   --scopes <csv>            API key scopes (default: read,write)
#   --dry-run                  Show actions without executing
#
set -euo pipefail

# --- Defaults --------------------------------------------------------------

BASE_URL="${ENGRAM_BASE_URL:-https://engram.zutfen.com}"
ADMIN_URL="${ENGRAM_ADMIN_URL:-$BASE_URL}"
ADMIN_KEY="${ENGRAM_ADMIN_API_KEY:-}"
TENANT_ID=""
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

# --- Arg parsing -----------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile)        PROFILE_NAME="$2"; shift 2 ;;
        --base-url)       BASE_URL="$2"; shift 2 ;;
        --admin-url)      ADMIN_URL="$2"; shift 2 ;;
        --admin-key)      ADMIN_KEY="$2"; shift 2 ;;
        --tenant-id)      TENANT_ID="$2"; shift 2 ;;
        --principal-name) PRINCIPAL_NAME="$2"; shift 2 ;;
        --scopes)         SCOPES="$2"; shift 2 ;;
        --hermes-home)    HERMES_HOME="$2"; shift 2 ;;
        --venv)           ENGRAM_HOOKS_VENV="$2"; shift 2 ;;
        --dry-run)        DRY_RUN=true; shift ;;
        -h|--help)        grep '^#' "$0" | head -25; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# --- Validation ------------------------------------------------------------

[[ -z "$PROFILE_NAME" ]] && { echo "ERROR: --profile is required"; exit 1; }

if [[ -z "$ADMIN_KEY" ]]; then
    ENV_FILE="$HERMES_HOME/profiles/engram/.env"
    if [[ -f "$ENV_FILE" ]]; then
        ADMIN_KEY=$(grep ENGRAM_API_KEY "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)
    fi
fi
[[ -z "$ADMIN_KEY" ]] && { echo "ERROR: No admin key. Use --admin-key or set ENGRAM_ADMIN_API_KEY"; exit 1; }

[[ -z "$PRINCIPAL_NAME" ]] && PRINCIPAL_NAME="${PROFILE_NAME}-agent"

PROFILE_DIR="$HERMES_HOME/profiles/$PROFILE_NAME"
[[ ! -d "$PROFILE_DIR" ]] && { echo "ERROR: Profile dir not found: $PROFILE_DIR"; exit 1; }

PLUGIN_DIR="$HERMES_HOME/plugins/engram_memory"
REPO_PLUGIN="$HOME/code/engram/adapters/engram-hooks/hermes_plugin/engram_memory"

echo "Engram Profile Onboarding"
echo "=========================="
echo "  Profile:       $PROFILE_NAME"
echo "  Engram URL:    $BASE_URL"
echo "  Admin URL:     $ADMIN_URL"
echo "  Principal:     $PRINCIPAL_NAME"
echo "  Scopes:        $SCOPES"
echo "  Dry run:       $DRY_RUN"
echo ""

# --- Step 1: Discover tenant + create principal ----------------------------

echo "[1/4] Setting up principal '$PRINCIPAL_NAME'..."

# Auto-discover tenant_id from the admin key via /whoami
if [[ -z "$TENANT_ID" ]]; then
    TENANT_ID=$(curl -sf "$ADMIN_URL/whoami" \
        -H "Authorization: Bearer $ADMIN_KEY" \
        | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tenant_id', ''))
except Exception:
    pass
" 2>/dev/null || echo "")

    if [[ -z "$TENANT_ID" ]]; then
        echo "  ERROR: Could not auto-discover tenant_id from /whoami."
        echo "  Check that the admin URL ($ADMIN_URL) is reachable and the key has read scope."
        echo "  Or provide --tenant-id explicitly."
        exit 1
    fi
fi
echo "  Tenant: $TENANT_ID"

# Check if principal already exists
EXISTING_PRINCIPAL_ID=$(curl -sf "$ADMIN_URL/v1/admin/principals?tenant_id=$TENANT_ID" \
    -H "Authorization: Bearer $ADMIN_KEY" \
    | python3 -c "
import sys, json
data = json.load(sys.stdin)
principals = data if isinstance(data, list) else data.get('items', [])
for p in principals:
    if p.get('name') == '$PRINCIPAL_NAME':
        print(p['id'])
        break
" 2>/dev/null || echo "")

if [[ -n "$EXISTING_PRINCIPAL_ID" ]]; then
    PRINCIPAL_ID="$EXISTING_PRINCIPAL_ID"
    echo "  Principal exists: $PRINCIPAL_ID"
else
    PRINCIPAL_PAYLOAD="{\"tenant_id\": \"$TENANT_ID\", \"name\": \"$PRINCIPAL_NAME\", \"type\": \"agent\"}"
    if $DRY_RUN; then
        echo "  [dry-run] Would create principal: $PRINCIPAL_PAYLOAD"
        PRINCIPAL_ID="dry-run"
    else
        PRINCIPAL_ID=$(curl -sf "$ADMIN_URL/v1/admin/principals" \
            -H "Authorization: Bearer $ADMIN_KEY" \
            -H "Content-Type: application/json" \
            -d "$PRINCIPAL_PAYLOAD" \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
        [[ -z "$PRINCIPAL_ID" ]] && { echo "  ERROR: Failed to create principal"; exit 1; }
        echo "  Created principal: $PRINCIPAL_ID"
    fi
fi

# --- Step 2: Create API key ------------------------------------------------

echo ""
echo "[2/4] Creating API key..."

SCOPES_JSON=$(python3 -c "print('[' + ','.join(repr(s.strip()) for s in '$SCOPES'.split(',')) + ']')")
KEY_PAYLOAD="{\"principal_id\": \"$PRINCIPAL_ID\", \"scopes\": $SCOPES_JSON}"

if $DRY_RUN; then
    echo "  [dry-run] Would create key with scopes: $SCOPES"
    NEW_API_KEY="eng_dryrun_placeholder"
else
    NEW_API_KEY=$(curl -sf "$ADMIN_URL/v1/admin/api-keys" \
        -H "Authorization: Bearer $ADMIN_KEY" \
        -H "Content-Type: application/json" \
        -d "$KEY_PAYLOAD" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null || echo "")
    [[ -z "$NEW_API_KEY" ]] && { echo "  ERROR: Failed to create API key"; exit 1; }
    echo "  API key: ${NEW_API_KEY:0:12}...${NEW_API_KEY: -4}"
fi

# --- Step 3: Install plugin + config ---------------------------------------

echo ""
echo "[3/4] Installing plugin + config..."

if $DRY_RUN; then
    echo "  [dry-run] Would copy plugin + write .env + update config.yaml"
else
    # Plugin
    if [[ -d "$REPO_PLUGIN" ]]; then
        mkdir -p "$PLUGIN_DIR"
        cp -r "$REPO_PLUGIN"/* "$PLUGIN_DIR/"
        echo "  Plugin copied from repo"
    elif [[ -d "$PLUGIN_DIR" ]]; then
        echo "  Plugin already installed"
    else
        echo "  WARNING: Plugin source not found. Copy manually from engram repo."
    fi

    # .env
    ENV_FILE="$PROFILE_DIR/.env"
    touch "$ENV_FILE"
    grep -q "^ENGRAM_BASE_URL=" "$ENV_FILE" && sed -i "s|^ENGRAM_BASE_URL=.*|ENGRAM_BASE_URL=$BASE_URL|" "$ENV_FILE" || echo "ENGRAM_BASE_URL=$BASE_URL" >> "$ENV_FILE"
    grep -q "^ENGRAM_API_KEY=" "$ENV_FILE" && sed -i "s|^ENGRAM_API_KEY=.*|ENGRAM_API_KEY=$NEW_API_KEY|" "$ENV_FILE" || echo "ENGRAM_API_KEY=$NEW_API_KEY" >> "$ENV_FILE"
    echo "  .env updated"

    # config.yaml — set provider + env vars
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
            sed -i "/^memory:/a\\  ENGRAM_BASE_URL: $BASE_URL\n  ENGRAM_HOOKS_COMPAT_SHIM: true\n  ENGRAM_HOOKS_REQUIRE_AUTOMATIC_CAPTURE: false" "$CONFIG_FILE"
        fi
        echo "  config.yaml updated"
    fi
fi

# --- Step 4: engram-hooks in venv ------------------------------------------

echo ""
echo "[4/4] Checking engram-hooks..."
if [[ -d "$ENGRAM_HOOKS_VENV" ]]; then
    if "$ENGRAM_HOOKS_VENV/bin/pip" show engram-hooks &>/dev/null; then
        echo "  Already installed"
    elif $DRY_RUN; then
        echo "  [dry-run] Would install engram-hooks"
    else
        "$ENGRAM_HOOKS_VENV/bin/pip" install engram-hooks 2>&1 | tail -1
        echo "  Installed"
    fi
else
    echo "  WARNING: Venv not found at $ENGRAM_HOOKS_VENV — install engram-hooks manually"
fi

# --- Done ------------------------------------------------------------------

echo ""
echo "=========================="
echo "Onboarding complete!"
echo ""
echo "Next steps:"
echo "  1. Restart the profile: hermes -p $PROFILE_NAME"
echo "  2. Check startup log for: 'Memory provider engram_memory registered'"
echo "  3. Write a test memory and verify it in Engram"
