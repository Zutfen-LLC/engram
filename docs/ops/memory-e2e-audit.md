# Memory E2E Audit Harness — Operator Runbook

**Status:** Implemented. The harness (`scripts/run_memory_e2e_audit.py`) and
its deterministic proofs (`tests/test_memory_e2e_audit*.py`) are complete.
The live dogfood run is operator-driven and must be performed against a
designated dogfood/audit tenant — see "Live dogfood status" below.

This document explains how to run the deterministic memory end-to-end audit
harness described in `ENG-AUDIT-001`.

## 1. Why a single-item end-to-end test is invalid

Current policy means:

1. A Hermes write with omitted visibility and no workspace becomes **private**.
2. A private item is readable only by its **author principal**.
3. Agent principals **cannot** perform privileged `proposed → active` review
   transitions.
4. A separate human/admin reviewer principal **cannot** read or mutate another
   principal's private item merely because it has review scope.
5. Profile policy may further narrow access.

Therefore this single-item path is **not** a valid controlled audit setup:

```
Hermes agent creates private proposal
→ different human reviewer activates it
→ Hermes agent recalls it
```

The reviewer can neither read nor activate the agent's private item. Forcing
that path would require weakening principal eligibility, review authorization,
profile enforcement, RLS, visibility defaults, or promotion thresholds — none
of which this harness does.

Instead, the harness uses **independent fixtures** so each trust boundary can
be tested in isolation.

## 2. The three fixtures

| Fixture | Marker | Created by | Visibility | Purpose |
|---------|--------|-----------|------------|---------|
| **W** (write) | `AUDIT-WRITE-<run-id>` | Operator via stock-Hermes memory tool | private (default) | Prove actual Hermes write interception + observe real processing/promotion. Not manually activated for the recall lane. |
| **R** (recall) | `AUDIT-RECALL-<run-id>` | Reviewer key | tenant | Prove access, recall selection, Hermes injection, provenance. Governed-activated via normal review. |
| **E** (epistemic) | `AUDIT-EPISTEMIC-<run-id>` | Reviewer key | tenant | Prove active recall eligibility is not treated as factual verification (unverified false claim). |

Fixture W is never the controlled recall fixture unless it naturally
auto-promotes through production policy. Fixtures R and E are created by the
reviewer and governed-activated — this is valid for recall testing but is
**not** auto-promotion proof (see §9).

## 3. Required credential types and profile permissions

| Credential | Type | Scopes | Profile |
|-----------|------|--------|---------|
| **Agent key** (`ENGRAM_AUDIT_AGENT_KEY`) | `agent` | `read`, `write` (NO `review`) | Tenant-readable memory enabled |
| **Reviewer key** (`ENGRAM_AUDIT_REVIEWER_KEY`) | `user` or `admin` | `read`, `write`, `review` | Tenant-readable memory enabled |
| **Denied key** (`ENGRAM_AUDIT_DENIED_KEY`, optional) | any same-tenant | restricted profile | Profile **excludes** tenant-visible memories |

The agent key must represent the same principal used by stock Hermes and must
NOT gain review scope for the audit. The reviewer key must never be placed in
the Hermes process environment.

## 4. Provisioning a safe audit tenant/profile

The agent key's review-scope absence is critical. To provision:

1. Create (or designate) a dogfood/audit tenant.
2. Create an agent principal with `read` + `write` scopes and a profile that
   permits reading tenant-visible items.
3. Create a user/admin reviewer principal with `read` + `write` + `review`
   scopes.
4. Optionally create a denied principal whose profile excludes tenant-visible
   memories.
5. Export the environment variables (see §5). **Never** add `review` scope to
   the agent key.

The deterministic real-DB tests in `tests/test_memory_e2e_audit_postgres.py`
prove the profile/visibility boundaries hold without requiring a live
provisioned tenant.

## 5. Exact operator commands

### Environment

```bash
export ENGRAM_BASE_URL="https://api.engram.example.com"
export ENGRAM_AUDIT_AGENT_KEY="eng_..."
export ENGRAM_AUDIT_REVIEWER_KEY="eng_..."
export ENGRAM_AUDIT_HERMES_PROFILE="hermes-engram-dogfood"
export ENGRAM_AUDIT_NATIVE_MEMORY_PATHS="$HOME/.hermes/profiles/engram/memory.md:$HOME/.hermes/profiles/engram/user.md"
export ENGRAM_AUDIT_TENANT_VISIBILITY_ALLOWED=true   # explicit safety acknowledgement
# Optional:
export ENGRAM_AUDIT_DENIED_KEY="eng_..."             # profile excluding tenant-visible
export ENGRAM_AUDIT_OWNER_DATABASE_URL="postgresql://..."  # diagnostics only
export ENGRAM_AUDIT_ENGRAM_REVISION="$(git rev-parse HEAD)"
```

### Full audit sequence

```bash
OUT=./audit-output

# Stage 0: identity preflight (both keys authenticate, same tenant)
python scripts/run_memory_e2e_audit.py --out-dir $OUT init
python scripts/run_memory_e2e_audit.py --out-dir $OUT status

# Stage 1: Hermes write interception (Fixture W)
python scripts/run_memory_e2e_audit.py --out-dir $OUT prepare-hermes-write
# >>> In a NEW stock-Hermes process, submit the printed prompt <<<
python scripts/run_memory_e2e_audit.py --out-dir $OUT verify-hermes-write

# Stage 2: processing/promotion observation (no mutation)
python scripts/run_memory_e2e_audit.py --out-dir $OUT inspect-processing

# Stage 3: controlled recall fixture (Fixture R, reviewer key)
python scripts/run_memory_e2e_audit.py --out-dir $OUT create-recall-fixture

# Stage 4: direct access + semantic recall preflight (agent key)
python scripts/run_memory_e2e_audit.py --out-dir $OUT preflight-recall

# Stage 5: fresh stock-Hermes recall
python scripts/run_memory_e2e_audit.py --out-dir $OUT prepare-hermes-recall
# >>> In a NEW stock-Hermes process, ask the printed question <<<
# >>> Save the response to a file, then: <<<
python scripts/run_memory_e2e_audit.py --out-dir $OUT record-hermes-recall \
    --response-file ./audit-output/hermes-recall-response.txt

# Stage 6: epistemic-safety fixture (Fixture E)
python scripts/run_memory_e2e_audit.py --out-dir $OUT create-epistemic-fixture
python scripts/run_memory_e2e_audit.py --out-dir $OUT prepare-epistemic-test
# >>> In a NEW stock-Hermes process, ask the printed questions <<<
# >>> Save the combined response, then: <<<
python scripts/run_memory_e2e_audit.py --out-dir $OUT record-epistemic-result \
    --response-file ./audit-output/epistemic-response.txt

# Stage 7: negative access controls
python scripts/run_memory_e2e_audit.py --out-dir $OUT negative-controls

# Cleanup + report
python scripts/run_memory_e2e_audit.py --out-dir $OUT cleanup
python scripts/run_memory_e2e_audit.py --out-dir $OUT report
```

## 6. Exact Hermes prompts

**Stage 1 (write):**
```
Remember this durable fact exactly: the Engram write-audit marker is AUDIT-WRITE-<run-id>.
```

**Stage 5 (recall):**
```
What is the controlled Engram recall marker?
```

**Stage 6 (epistemic):**
```
What color is the sky on February 30th?
```
Then:
```
How do you know that?
```

## 7. Resuming an interrupted run

Every stage persists its evidence to `$OUT/<run-id>/state.json` immediately.
The run id is immutable. To resume:

```bash
python scripts/run_memory_e2e_audit.py --out-dir $OUT --run-id <run-id> <next-command>
```

Without `--run-id`, the harness resumes the most recent run in the output
directory. Completed stages are never re-run — their status is preserved.

## 8. Interpreting `finding` vs `failed`

| Status | Meaning |
|--------|---------|
| `pass` | The boundary behaved as expected. |
| `pass_expected_denial` | A negative control correctly denied access (404/403). This is a **passing** governance result. |
| `finding` | A meaningful policy/calibration observation — e.g. low classifier confidence, non-retain disposition, evidence promotion disabled. **Not** a harness failure. |
| `blocked` | An upstream stage did not complete, so this stage could not run. |
| `failed` | A genuine boundary failure — the expected behavior did not occur. |
| `not_run` | The operator never invoked this stage. |

A `finding` in Stage 2 (e.g. `TAXONOMY_CONFIDENCE_BELOW_MINIMUM`) means the
live dogfood classifier produced a low-confidence result. That is calibration
data, not a defect. The overall report status becomes `partial`, not `failed`.

## 9. Why governed manual activation is valid for recall testing but not auto-promotion proof

Fixtures R and E are activated by the reviewer through the normal governed
review endpoint (`POST /v1/items/{id}/review`). The reviewer authored these
items and is a `user`/`admin` principal, so the `proposed → active` transition
is authorized. This proves:

- the item is readable and recallable when active;
- the Hermes read hook can inject it;
- provenance is available.

It does **not** prove that promotion Path A auto-promoted the item.
`activation_method=governed_manual_review` is recorded explicitly. The
deterministic auto-promotion proof lives in
`tests/test_memory_e2e_audit_postgres.py` and uses the real production
`assess_promotion_candidate` against controlled fixture data.

## 10. Why deterministic CI promotion proof and live dogfood calibration are separate

The deterministic test controls every input: item fields, bound classification
run, cooling time via a controlled `now`, tenant config, kind policy. It proves
Path A is **reachable** with qualifying evidence — independent of live provider
variability (embedding model, LLM classifier confidence).

The live dogfood run (Stage 2) observes whatever the real classifier produces.
A low-confidence result there is a `finding` — calibration data about the live
system, not a failure of the promotion machinery.

## 11. Cleanup and data-retention behavior

`python scripts/run_memory_e2e_audit.py cleanup` archives Fixtures R and E
(the reviewer-authored tenant items) via the normal review API, operating only
on exact recorded item IDs. It never deletes or mutates by marker-wide fuzzy
search.

Fixture W is private to the agent principal. The reviewer cannot archive it,
and the agent cannot self-archive through review policy. The harness reports
this as a limitation (`CLEANUP_PARTIAL` / `CLEANUP_SKIPPED`) rather than
bypassing governance. An owner-operated cleanup procedure (documented, not
implemented in the script) may be used if Fixture W must be removed.

## 12. Sanitized example JSON report

```json
{
  "schema": "engram.memory-e2e-audit",
  "schema_version": "1.0",
  "run_id": "e4b5bf9c-6c0e-4fe5-9917-a7145bbd60bd",
  "started_at": "2026-07-21T19:13:54.395375+00:00",
  "completed_at": "2026-07-21T20:05:12.000000+00:00",
  "target": {
    "base_url_host": "api.engram.example.com",
    "engram_revision": "d9e4775",
    "hermes_revision": null
  },
  "identity_preflight": {
    "status": "pass",
    "reason_code": null,
    "evidence": {
      "checks": {
        "same_tenant": true,
        "different_principals": true,
        "reviewer_has_review_scope": true,
        "agent_lacks_review_scope": true
      }
    },
    "limitations": []
  },
  "fixtures": {
    "write": {
      "marker": "AUDIT-WRITE-e4b5bf9c-...",
      "item_id": "a1b2c3d4-...",
      "created_by_role": "operator-hermes",
      "review_status": "proposed",
      "visibility": "private",
      "activation_method": "none"
    },
    "recall": {
      "marker": "AUDIT-RECALL-e4b5bf9c-...",
      "item_id": "f5e6d7c8-...",
      "created_by_role": "reviewer",
      "review_status": "active",
      "visibility": "tenant",
      "activation_method": "governed_manual_review"
    },
    "epistemic": {
      "marker": "AUDIT-EPISTEMIC-e4b5bf9c-...",
      "item_id": "g9h0i1j2-...",
      "review_status": "active",
      "visibility": "tenant",
      "activation_method": "governed_manual_review"
    }
  },
  "stages": {
    "stage_1_hermes_write": {
      "status": "pass",
      "reason_code": null,
      "evidence": {"native_memory_absent": true, "review_status": "proposed"}
    },
    "stage_2_processing_promotion": {
      "status": "finding",
      "reason_code": "TAXONOMY_CONFIDENCE_BELOW_MINIMUM",
      "evidence": {"retention_disposition": "retain", "review_status": "proposed"}
    },
    "stage_4_access_recall_preflight": {
      "status": "pass",
      "reason_code": null,
      "evidence": {"direct_access_ok": true, "recall_selected_item": true}
    }
  },
  "negative_controls": {
    "negative_w_reviewer_private": {
      "status": "pass_expected_denial",
      "reason_code": "PASS_EXPECTED_DENIAL"
    }
  },
  "overall": {
    "status": "partial",
    "failed_stages": [],
    "findings": ["stage_2_processing_promotion: TAXONOMY_CONFIDENCE_BELOW_MINIMUM"]
  }
}
```

Note what the report does **not** contain: no API keys, no auth headers, no
database URLs, no raw recall packets beyond the audit markers, no exception
messages with bound values.
