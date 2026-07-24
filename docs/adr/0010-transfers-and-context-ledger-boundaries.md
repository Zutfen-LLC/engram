# ADR 0010 — Transfers and Context Ledger Boundaries

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A (decision; no implementation this slice)

## Context

Customers will eventually want to move data (on import, on org merge/split, on
export). The Context Ledger (startup receipts, dark-write evidence) is Core-owned
and authoritative; its provenance must not be bypassed by hosted workflows.

## Decision

- **Transfers are future asynchronous Control Plane workflows** (import/export,
  org-to-org moves). They are not synchronous Portal actions and do not write
  memory trust state directly.
- **Context Ledger data remains owned by Core** and is accessed through
  **authorized Core APIs**, never by the Control Plane or Portal reading Core
  tables.
- **Context Receipt verification is not factual certification or proof of
  causality.** A receipt proves the receipt itself, not that a specific agent
  run caused a specific recall outcome.

## Security and authorization consequences

- Hosted workflows cannot silently rewrite provenance or trust.
- Receipt semantics are bounded and honestly labeled.

## Operational consequences

- Transfers will be long-running, observable jobs.

## Rejected alternatives

- **Direct Control-Plane writes to Core tables for transfers.** Rejected: breaks
  the data boundary and provenance integrity.

## Deferred follow-up slices

- Transfer workflows and Core transfer/import/export APIs (future).
