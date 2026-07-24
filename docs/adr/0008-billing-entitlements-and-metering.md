# ADR 0008 — Billing, Entitlements, and Metering

- **Status:** Accepted
- **Date:** 2026-07-24
- **Scope:** ENG-PORTAL-001A (decision; no implementation this slice)

## Context

A hosted product needs payments, subscriptions, entitlements, and usage meters.
We must be precise about what billing can and cannot do to memory trust.

## Decision

- **Stripe owns payments** (checkout, invoices, payment methods).
- **Engram owns entitlements** (what a customer is allowed to do), derived from —
  but not blindly equal to — Stripe subscription state.
- **Diagnostic usage is not automatically billable usage.** Authoritative
  billable meters are owned by the Control Plane.
- **Billing restrictions never silently alter memory trust, ranking, recall,
  visibility, RLS, or delete data.** A lapsed subscription may restrict new
  writes or surface warnings, but it does not silently downgrade or remove
  stored memory trust state. There is no automatic data deletion on downgrade.
- No Stripe SDK, webhook, subscription, entitlement, quota, or billable-meter
  table is implemented this slice. Billing status is `not_configured`.

## Security and authorization consequences

- Billing state cannot be weaponized to silently compromise memory integrity.
- Customer content is not sent to the billing provider.

## Operational consequences

- Entitlement evaluation will be a Control Plane responsibility.

## Rejected alternatives

- **Stripe as the source of truth for entitlements.** Rejected: entitlements
  need richer, Engram-specific semantics than a subscription row.
- **Auto-downgrade of memory trust on non-payment.** Rejected: violates the trust
  model.

## Deferred follow-up slices

- Billing/entitlement/meter implementation (post-ENG-DELEGATION-001).
