# Dogfood usage-metering runbook (ENG-METER-001)

Enables usage/metering telemetry on an existing dogfood deployment and
produces the first reports. See `docs/usage-metering.md` for the event
taxonomy, privacy rules, and known limitations — this file is the operational
checklist only.

> This is an OBSERVABILITY rollout, not a pricing/billing rollout. Nothing
> here enforces quotas or changes what the service does for a caller.

---

## 1. Apply the migration

```bash
# Compose deployment: the migration ships in migrations/ and applies via
# `engram init-db` (idempotent — safe to re-run).
docker compose exec engram-service engram init-db

# Or directly against the owner role, if running the CLI from a checkout:
engram init-db --database-url "$ENGRAM_OWNER_DATABASE_URL"
```

Verify the table landed and RLS is forced:

```bash
docker compose exec postgres psql -U "${POSTGRES_OWNER_USER:-engram}" -d engram -c \
  "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'usage_events';"
# expect: t | t
```

## 2. Set `ENGRAM_USAGE_TELEMETRY_ENABLED=true`

Add to the deployment `.env`:

```bash
ENGRAM_USAGE_TELEMETRY_ENABLED=true
```

This flag is read by both the API and worker services (`docker-compose.yml`'s
shared `x-env` anchor already propagates it to both).

## 3. Enable hooks lifecycle-summary telemetry

On the dogfood client/profile running `engram-hooks`:

```bash
ENGRAM_HOOKS_REPORT_LIFECYCLE_TELEMETRY=true
```

This is independent of step 2 — both must be set for the candidate funnel
(section B of the report) to include lifecycle-extraction/guard-rejection
figures alongside server-observed candidates.

## 4. Rebuild/restart API and worker

```bash
docker compose up -d --build engram-service engram-worker
```

## 5. Verify one capture of each kind

Run one of each operation and confirm a row lands in `usage_events`:

```bash
# One remember (produces candidate.observed + candidate.outcome)
curl -s -H "authorization: Bearer $ENGRAM_API_KEY" -H "content-type: application/json" \
  -d '{"content":"dogfood usage-metering smoke test","source_type":"manual"}' \
  http://localhost:8000/v1/remember | jq .

# One semantic recall (produces retrieval.request; embedding_query_recall
# provider.call only if ENGRAM_EMBEDDING_PROVIDER != none)
curl -s -H "authorization: Bearer $ENGRAM_API_KEY" -H "content-type: application/json" \
  -d '{"mode":"semantic","query":"usage-metering smoke test"}' \
  http://localhost:8000/v1/recall | jq .

# One search (produces retrieval.request for keyword/semantic/hybrid)
curl -s -H "authorization: Bearer $ENGRAM_API_KEY" -H "content-type: application/json" \
  -d '{"query":"usage-metering smoke test","mode":"hybrid"}' \
  http://localhost:8000/v1/search | jq .

# Confirm rows landed (owner role, bypasses RLS)
docker compose exec postgres psql -U "${POSTGRES_OWNER_USER:-engram}" -d engram -c \
  "SELECT event_type, operation, status, count(*) FROM usage_events
   WHERE created_at > now() - interval '10 minutes'
   GROUP BY 1, 2, 3 ORDER BY 1, 2;"
```

If a lifecycle-hook-driven capture is available (Hermes session running with
engram-hooks configured per step 3), trigger a `sync_turn` or `session_end`
and confirm a `client.lifecycle_summary` row appears the same way.

## 6. Run the 24-hour report

```bash
engram usage-report --since "$(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ)" --json \
  > /path/to/protected/dir/usage-report-$(date -u +%Y%m%d).json
```

Sanity-check `coverage.telemetry_enabled` is `true` and
`coverage.first_event_at`/`last_event_at` fall inside the smoke-test window
from step 5.

## 7. Run the seven-day report

```bash
engram usage-report --json > /path/to/protected/dir/usage-report-7d-$(date -u +%Y%m%d).json
# Human-readable version for a quick read:
engram usage-report
```

## 8. Save reports outside the public repository

Reports may contain principal IDs, tenant IDs, token/cost figures, and
per-principal candidate volumes — treat them as operator-internal by default.

- Write reports to an operator-owned directory outside any repo working tree
  (e.g. `/srv/engram-reports/`, mode `700`, owned by the operator account).
- **Never commit an unsanitized report to this repository.** If a report is
  needed for a PR description or shared documentation, redact principal/
  tenant identifiers and review the numbers for anything sensitive first.

## 9. Never fabricate live evidence when the dogfood host is unavailable

If you cannot reach the dogfood host to run steps 5–7, say so explicitly in
whatever report/PR you're writing. Do not present fixture-derived or
hypothetical numbers as if they were captured live. A sample report generated
from test fixtures (for documentation purposes) must be clearly labeled as
such.

---

## Suggested daily cron / systemd invocation

Writes one JSON report per day to a protected, operator-owned directory.

### cron

```cron
# /etc/cron.d/engram-usage-report — runs as the operator account, not root.
15 0 * * * engram-operator ENGRAM_DATABASE_URL=... ENGRAM_OWNER_DATABASE_URL=... \
  /opt/engram/.venv/bin/engram usage-report --since "$(date -u -d '24 hours ago' +\%Y-\%m-\%dT\%H:\%M:\%SZ)" --json \
  > /srv/engram-reports/usage-report-$(date -u +\%Y\%m\%d).json 2>> /srv/engram-reports/usage-report.log
```

### systemd (timer + service)

```ini
# /etc/systemd/system/engram-usage-report.service
[Unit]
Description=Engram daily usage-metering report

[Service]
Type=oneshot
User=engram-operator
EnvironmentFile=/opt/engram/.env
WorkingDirectory=/opt/engram
ExecStart=/bin/sh -c '/opt/engram/.venv/bin/engram usage-report \
  --since "$(date -u -d "24 hours ago" +%%Y-%%m-%%dT%%H:%%M:%%SZ)" --json \
  > /srv/engram-reports/usage-report-$(date -u +%%Y%%m%%d).json'
```

```ini
# /etc/systemd/system/engram-usage-report.timer
[Unit]
Description=Run engram-usage-report daily

[Timer]
OnCalendar=*-*-* 00:15:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now engram-usage-report.timer
```

`/srv/engram-reports/` should be created with `mode 700`, owned by the
operator account running the timer/cron job — not world-readable, and not
inside a git working tree.
