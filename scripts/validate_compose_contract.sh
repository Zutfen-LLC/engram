#!/usr/bin/env bash
# Validate the resolved production Compose service contract.

set -euo pipefail

# `docker compose config --services` parses and validates the full file, so a
# preceding `config -q` would only repeat the same work.
services=$(docker compose config --services)
echo "Services: $services"

if ! grep -qx 'engram-worker' <<<"$services"; then
  echo "ERROR: engram-worker service not found in resolved compose config" >&2
  exit 1
fi

# Representative shared and API-only values prove that the x-env anchor and
# service-specific environment blocks preserve their intended boundary.
export ENGRAM_VOCAB_CACHE_TTL_SECONDS=999
export ENGRAM_VOCAB_CACHE_MAX_TENANTS=888
export ENGRAM_PROMOTION_CONFLICT_CANDIDATE_K=777
export ENGRAM_USAGE_TELEMETRY_ENABLED=true
export ENGRAM_API_KEY_CACHE_TTL_SECONDS=666
export ENGRAM_READ_DATABASE_URL='postgresql+asyncpg://sentinel-read'
export ENGRAM_RECALL_BYTE_BUDGET=555
export ENGRAM_STARTUP_PROMOTION_LIMIT=444

api_config=$(docker compose config engram-service)
worker_config=$(docker compose config engram-worker)

require_env() {
  local service=$1
  local config=$2
  local key=$3
  local expected=$4

  if ! awk -v key="$key" -v expected="$expected" '
    $1 == key ":" {
      value = substr($0, index($0, ":") + 1)
      sub(/^[[:space:]]+/, "", value)
      sub(/[[:space:]]+$/, "", value)
      if ((substr(value, 1, 1) == "\"" && substr(value, length(value), 1) == "\"") ||
          (substr(value, 1, 1) == "\047" && substr(value, length(value), 1) == "\047")) {
        value = substr(value, 2, length(value) - 2)
      }
      found = (value == expected)
    }
    END { exit !found }
  ' <<<"$config"; then
    echo "ERROR: $key=$expected missing/wrong on $service" >&2
    exit 1
  fi
}

reject_env() {
  local service=$1
  local config=$2
  local key=$3

  if awk -v key="$key" '$1 == key ":" { found = 1 } END { exit !found }' <<<"$config"; then
    echo "ERROR: $key should NOT be on $service" >&2
    exit 1
  fi
}

# Compose emits command in block-list form. Bound collection to the command
# field so unrelated YAML list items cannot satisfy the assertion.
worker_command=$(awk '
  /^[[:space:]]+command:[[:space:]]*$/ {
    in_command = 1
    command_indent = index($0, "c") - 1
    next
  }
  in_command {
    match($0, /^[[:space:]]*/)
    if (RLENGTH <= command_indent) { exit }
    if ($1 == "-") {
      value = substr($0, index($0, "-") + 1)
      sub(/^[[:space:]]+/, "", value)
      gsub(/^[\047"]|[\047"]$/, "", value)
      command = command (command ? " " : "") value
    }
  }
  END { print command }
' <<<"$worker_config")
if [[ $worker_command != "engram worker" ]]; then
  echo "ERROR: expected worker command 'engram worker', got '$worker_command'" >&2
  exit 1
fi
echo "Worker command: $worker_command"

# Reject a ports key at the worker service's immediate child depth.
if awk '
  /^[[:space:]]+engram-worker:[[:space:]]*$/ {
    service_indent = index($0, "e") - 1
    in_service = 1
    next
  }
  in_service {
    match($0, /^[[:space:]]*/)
    if (NF && RLENGTH <= service_indent) { exit }
    if (RLENGTH == service_indent + 2 && $1 == "ports:") { found = 1 }
  }
  END { exit !found }
' <<<"$worker_config"; then
  echo "ERROR: engram-worker must not publish ports" >&2
  exit 1
fi
echo "Worker published ports: 0"

for entry in \
  'ENGRAM_VOCAB_CACHE_TTL_SECONDS=999' \
  'ENGRAM_VOCAB_CACHE_MAX_TENANTS=888' \
  'ENGRAM_PROMOTION_CONFLICT_CANDIDATE_K=777' \
  'ENGRAM_USAGE_TELEMETRY_ENABLED=true'; do
  key=${entry%%=*}
  expected=${entry#*=}
  require_env engram-service "$api_config" "$key" "$expected"
  require_env engram-worker "$worker_config" "$key" "$expected"
done
echo "Shared sentinel checks: 4 passed"

for entry in \
  'ENGRAM_API_KEY_CACHE_TTL_SECONDS=666' \
  'ENGRAM_READ_DATABASE_URL=postgresql+asyncpg://sentinel-read' \
  'ENGRAM_RECALL_BYTE_BUDGET=555' \
  'ENGRAM_STARTUP_PROMOTION_LIMIT=444'; do
  key=${entry%%=*}
  expected=${entry#*=}
  require_env engram-service "$api_config" "$key" "$expected"
  reject_env engram-worker "$worker_config" "$key"
done
echo "API-only sentinel checks: 4 passed"

require_env engram-service "$api_config" ENGRAM_JOB_MAX_ATTEMPTS 5
require_env engram-worker "$worker_config" ENGRAM_JOB_MAX_ATTEMPTS 5
echo "ENGRAM_JOB_MAX_ATTEMPTS present on both services"

echo "All Compose content assertions passed."
