# Paid-beta Core launch audit

**Audit date:** 2026-08-05  
**Audited revision:** [`41e31ae`](https://github.com/Zutfen-LLC/engram/commit/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6) (`origin/main`)  
**Question:** What in Engram Core is implemented and evidenced for a paid private beta,
and what still blocks an externally usable hosted release?

## Verdict

Engram's trust boundary is substantially stronger than its release boundary. Tenant
isolation, scoped authentication, append-first memory governance, multi-agent
provisioning, service delegation, and the REST/Python/MCP paths have meaningful
real-PostgreSQL coverage. The audited revision also passed the full official CI run,
including the real-database suite and production-image smoke test.

Core is **not yet launch-ready** for the agreed paid-beta contract. The principal
blockers are:

1. There is no complete, portable tenant export or lossless self-hosted import.
2. The hosted management authority is incomplete: Portal-safe invalidation,
   standalone agent deactivation/revocation/status, and a pageable review queue are
   missing or insufficient.
3. There is no supported operator workflow to purge a tenant after an account-deletion
   request and prove its expiry from backups.
4. The open-source product has no tagged release, published artifacts, immutable-image
   quickstart, upgrade proof, or vulnerability-reporting policy.
5. Hermes automatic lifecycle support lacks one unambiguous, version-pinned,
   full-process acceptance record; current docs conflict.
6. Production operations do not yet prove the launch targets: actionable monitoring,
   hourly recovery point, measured eight-hour recovery, beta-scale load, and enforced
   safety/usage limits.
7. The exact release candidate still needs an auth-enabled hosted staging acceptance
   and targeted independent security review with no unresolved critical/high findings.

Dedicated infrastructure, public signup, multi-workspace/group management,
self-service account deletion, and usage billing are not Core launch blockers for this
beta.

## Evidence rubric

| Proof | Meaning in this audit |
|---|---|
| **Implemented** | The behavior exists in production code. |
| **PG-tested** | An automated test exercises it against PostgreSQL/pgvector, including RLS where relevant. |
| **CI-proven** | The test or artifact is exercised by the official CI workflow, and the audited revision has a passing run. |
| **Live-proven** | A recorded deployment or real integration run proves the behavior outside the test harness. |
| **Externally usable** | A customer/operator can consume it from a versioned public artifact and supported documentation, without a source checkout or private workaround. |

The official [CI run for the audited revision](https://github.com/Zutfen-LLC/engram/actions/runs/30682181964)
passed the real-database merge-reference suite, runtime-image smoke test, Compose
validation, conformance vectors, and lock-drift jobs. “CI-proven” below inherits that
run only where the cited workflow actually includes the relevant test.

## Evidence matrix

| Capability | Implemented | PG-tested | CI-proven | Live-proven | Externally usable | Finding |
|---|---:|---:|---:|---:|---:|---|
| Tenant isolation, RLS, scopes, append-first governance | Yes | Yes | Yes | Partial | Partial | The CI runner verifies pgvector, the non-owner application role, FORCE RLS, and cross-tenant denial across the RLS tables. Older deployment records exist, but not for this exact revision. |
| Multi-agent creation, listing, and credential lifecycle | Yes | Yes | Yes | No current record | Partial | Hosted provisioning has stable workspace/agent identities, one-time secrets, atomic key replacement, and concurrency tests. A standalone hosted revoke/deactivate/status operation is missing. |
| Search, provenance inspection, review, and invalidation | Yes | Yes | Yes | Partial | Partial | Ordinary scoped APIs provide the underlying actions. The hosted delegation seam exposes read and purpose-bound review, but not invalidation. |
| Portal-safe read/review delegation | Partial | Yes | Yes | No current record | Partial | Read delegation is broad enough for management reads. Review delegation is purpose-bound and audited, but its queue is fixed at 50 and rejects query parameters, so it cannot safely exhaust a larger queue. |
| Python client and MCP | Yes | Yes | Yes | Partial | No | MCP has a real PostgreSQL end-to-end test and an older live record. Both SDK and MCP remain workspace packages installed from a source checkout; they are not published artifacts. |
| Hermes automatic lifecycle | Partial | Partial | Yes | Disputed | No | Hook/plugin behavior has tests, but the recorded live run notes a cached old module and other project status docs still call the full lifecycle gate outstanding. |
| Complete tenant export and self-hosted import | No | No | No | No | No | The current export is a filtered CCA-style view of active eligible memories, not a portable tenant archive. The importer creates new memories and is not its inverse. |
| Tenant deletion by operator request | No | No | No | No | No | Soft invalidation exists, and the schema reserves deletion events, but no supported tenant purge and backup-expiry workflow is implemented. |
| Health, backup, restore, monitoring | Partial | Partial | Partial | Stale/partial | No | Readiness and backup/restore scripts exist. The documented schedule is daily, recovery time is not measured, and worker queue/heartbeat alerting is explicitly future work. |
| Usage observability and safety limits | Partial | Yes | Yes | No current record | Partial | Usage receipts exist, but metering is off by default and deliberately does not enforce quotas. Request/body/rate/storage/provider-cost limits are not a launch-grade control plane yet. |
| Versioned open-source release | No | Partial | Partial | No | No | Package manifests and a production-image smoke test exist, but there are no tags/releases, published SDK/MCP packages, immutable image, release workflow, or security policy. |

### Trust and multi-agent evidence

The CI path deliberately runs migrations and tests with PostgreSQL 16/pgvector and a
non-owner application role, and fails if database-dependent tests skip. It verifies
FORCE RLS before running lint, strict typing, Core, SDK, MCP, hooks, and credential
scanning tests ([CI runner](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/scripts/run_ci.py#L77-L182),
[Compose roles](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docker-compose.ci.yml#L35-L119)).
The isolation suite enumerates the RLS-protected tables and proves cross-tenant and
missing-context denial with the application role
([RLS tests](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/tests/test_rls_isolation.py#L38-L64),
[cross-tenant cases](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/tests/test_rls_isolation.py#L332-L405)).

The hosted provisioning API is the strongest multi-agent evidence. It creates stable
tenant/human/workspace/agent resources, returns new agent credentials once, and
atomically replaces an old credential
([contract](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/service-provisioning.md#L3-L58),
[routes](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/api/routes/service_provisioning.py#L224-L562)).
Real-database tests cover replay stability, one-time disclosure, replacement, and
concurrent convergence
([PostgreSQL tests](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/tests/test_service_provisioning_postgres.py#L1151-L1248),
[concurrency tests](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/tests/test_service_provisioning_postgres.py#L1564-L1685)).
This is structurally suitable for ten agents per beta organization; the entitlement
limit belongs in the commercial control plane. Core still needs a service-authority
operation that can revoke/deactivate one managed agent without issuing a replacement,
plus a safe status read for reconciliation.

### Management-authority evidence

Core already implements memory search, item inspection, metadata governance,
supersession, invalidation, and review transitions
([memory routes](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/api/routes/memory.py#L1401-L1458),
[item/invalidation routes](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/api/routes/memory.py#L2042-L2449),
[review routes](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/api/routes/review.py#L166-L204)).
The question is not whether Core can perform these actions; it is whether the hosted
control plane can do so without holding or exposing ordinary user/agent credentials.

The service delegation boundary correctly limits a general delegated credential to
`read`, and gives review a purpose-bound `review.queue`/`review.transition` token
([delegation scopes](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/delegation_auth.py#L28-L36),
[review restrictions](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/delegation_auth.py#L204-L265)).
Tests prove the queue token cannot be reused for search/export/other routes and prove
audited, single-use transitions. They also prove the current problem: the queue caps
at 50, and adding query parameters is denied
([review delegation tests](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/tests/test_service_review_delegation_postgres.py#L689-L849)).
There is no purpose-bound hosted invalidation authority. These are launch blockers for
the minimum daily management contract, not merely Portal UI work.

### Portability and deletion evidence

The existing `/v1/export` route intentionally selects only currently valid,
read-eligible memories of four CCA kinds and emits a small content/metadata projection
([export implementation](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/api/routes/export.py#L1-L87)).
Its tests explicitly confirm that facts, observations, and inactive memories are
excluded
([export test](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/tests/test_export.py#L240-L285)).
It therefore cannot satisfy an always-available, full portable export containing all
agents/workspaces, every memory lifecycle state, provenance/events, graph structures,
feedback/conflicts, diaries, receipts, and a versioned checksummed manifest.

The CCA import script maps a small set of kinds and calls `remember`; it does not
preserve identifiers, lifecycle history, graph topology, or provenance, and is not a
lossless inverse
([importer](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/scripts/import_cca.py#L1-L64),
[write loop](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/scripts/import_cca.py#L125-L189)).

The design records hard deletion and sensitive-read auditing as deferred, despite a
`deletion_events` schema placeholder
([deferred deletion design](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/design.md#L1603-L1610)).
Self-service hard deletion of individual memories is not required for the beta, but an
authenticated operator must be able to purge an entire requested tenant, record the
operation, and prove the tenant ages out of retained backups.

### MCP and Hermes evidence

MCP's suite is meaningful: it exercises MCP → Python SDK → HTTP → FastAPI → real
PostgreSQL and is configured to fail rather than silently skip in CI
([MCP proof model](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/adapters/mcp-server/README.md#L140-L178),
[integration test](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/adapters/mcp-server/tests/test_integration.py#L1-L14)).
It is still distributed as an editable source checkout, and the root package manifest
explicitly describes `engram-client` as unpublished/workspace-local
([dependency note](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/pyproject.toml#L103-L109)).

The Hermes Gate C record reports accepted/rejected capture, recall, idempotence, and
restart checks, but also says the running process retained an old cached plugin module
and documents direct hook invocation limitations
([Gate C record](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/ops/gate-c-lifecycle-e2e-2026-07-13.md#L1-L33),
[limitations](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/ops/gate-c-lifecycle-e2e-2026-07-13.md#L99-L112)).
The current README still labels the real Hermes lifecycle gate as outstanding
([status](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/README.md#L588-L596)).
Before launch, run one clean, version-pinned Hermes process through startup, automatic
accepted and rejected capture, truthful attribution, review/promotion, restart
persistence, and recall; retain the evidence and make all status docs agree.

### Release and operational evidence

The repository has four alpha-version package manifests and a production runtime
image smoke path, but the runtime Docker build installs the root package directly
rather than consuming a locked, published release artifact
([Dockerfile](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/Dockerfile#L14-L20)).
At audit time, the public repository had no [tags](https://github.com/Zutfen-LLC/engram/tags),
no [releases](https://github.com/Zutfen-LLC/engram/releases), and no
[security policy](https://github.com/Zutfen-LLC/engram/security/policy). The release
must couple the hosted candidate to an immutable public Core version, with Core/SDK/MCP
artifacts, a clean-machine quickstart, upgrade and export-import compatibility proof,
license notices, supported-version policy, and a private vulnerability-reporting path.

Operationally, Core provides liveness/readiness checks, including database/RLS/pgvector
and service-authority readiness
([health routes](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/engram/api/routes/health.py#L52-L217)),
plus backup and destructive restore procedures
([backup script](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/deploy/backup.sh#L38-L103),
[restore runbook](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/deployment.md#L325-L423)).
However, the documented example schedule is daily rather than hourly, and the same
runbook says worker health checks, queue depth, heartbeat monitoring, and alerting are
planned
([daily schedule](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/deployment.md#L344-L350),
[worker gap](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/deployment.md#L578-L596)).
Usage receipts are observability rather than enforcement: metering is off by default
and explicitly does not implement quota enforcement
([usage contract](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/usage-metering.md#L1-L7),
[non-goals](https://github.com/Zutfen-LLC/engram/blob/41e31ae0b132f2aaf1a984bdea7c35a7e8df6dd6/docs/usage-metering.md#L311-L317)).

## Required launch gates

### Product/data gates

- **Portable export/import:** define a versioned archive schema; export every
  customer-owned record without credentials or credential hashes; add per-file or
  per-section checksums and a manifest; implement a lossless, idempotent self-hosted
  import; prove round-trip equivalence on a multi-agent tenant with every lifecycle
  state. Expose export through a narrowly scoped, Portal-safe authority.
- **Hosted management completion:** add purpose-bound invalidation; pageable/filterable
  review queue behavior; and service-authority agent status plus standalone
  deactivate/revoke. Exercise all of them with the real application role and negative
  cross-tenant/scope tests.
- **Operator deletion:** implement a runbook/tool that resolves an organization to all
  tenant-owned rows and external objects, previews scope, requires deliberate
  confirmation, purges transactionally where possible, records non-sensitive proof,
  and tracks backup expiry. Prove it on a restored backup.
- **Safety limits:** set and test launch defaults for request size, memory size,
  requests/rate, tenant storage, queue depth/concurrency, and provider spend. Expand
  credential rejection beyond the current narrow regex set, document its limitations,
  and align logs/telemetry with the prohibited-data policy.

### Release/evidence gates

- **Hermes acceptance:** complete the clean full-process test described above and make
  README/design/backlog/operations status consistent.
- **Versioned public release:** tag the candidate; publish immutable Core runtime,
  Python SDK, and MCP artifacts; prove clean-machine install/quickstart and one-version
  upgrade; publish security/support policy and release notes; verify license notices.
- **Production operations:** configure actionable API/worker/database/provider alerts;
  automate encrypted off-host backups at no more than one-hour intervals; restore the
  exact candidate and measure recovery below eight hours; document maintenance and
  business-hours incident handling.
- **Capacity:** exercise at least five isolated organizations and more than fifty
  agents under concurrent remember/search/review/export/worker load. Record latency,
  error rate, connection/queue saturation, storage growth, and recovery behavior.
- **Security:** obtain an independent targeted review of tenant isolation, auth/scopes,
  service credentials and delegation, SSRF/provider egress, secret handling,
  export/import, deletion, logs, and deployment boundaries. Resolve all critical/high
  findings and retain retest evidence.
- **Exact-candidate staging:** with auth enabled and production-like TLS/networking,
  prove invitation provisioning, two-tenant isolation, ten agents in one organization,
  management actions, rotation/revocation, MCP, Hermes, export/import, tenant purge,
  backup/restore, and monitoring. Pin the result to the released tag and image digest.

## Exit criterion

Core is ready for the first paid beta customer when every gate above has an owner,
passing evidence pinned to the same tagged release candidate, and no unresolved
critical/high security finding. Code presence or an older dogfood record alone is not
sufficient. The strongest existing foundation—the current real-PostgreSQL CI trust
suite—should remain the mandatory baseline for every release and upgrade.
