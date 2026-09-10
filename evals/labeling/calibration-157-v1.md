# Calibration labeling guide v1 (issue #202)

Version: `engram-calibration-guide-157-v1`. This guide is FROZEN before bulk
review begins. It extends — and must be read with — the admission labeling
handbook ([admission-v1.md](admission-v1.md)), whose dimension definitions,
vocabulary, and decision rules apply verbatim to every dimension reused here.
This document adds only what calibration review additionally requires.

## Purpose

Labels produced under this guide calibrate the #157 assessment dimensions
(taxonomy, retention, epistemic) for the dogfood shadow-evidence posture.
They are calibration evidence only. They do not certify governed recall, do
not authorize candidate serving, and never become production admissions.

## What the reviewer sees

Each reviewer works from a blind packet: item content, governed kind, source
type, review status, and recorded provenance facts. Reviewers never see:
provider scores, model suggestions, current admission/policy outputs, or the
other reviewer's labels. Judge from decision-time evidence in the packet only.

## Dimensions (reuse admission-v1 definitions)

Label every field of the `engram-admission-label-v1` Dimensions object.
Additionally required for calibration:

1. **expected_kind** — judge independently of the governed kind. The
   calibration question is whether the provider's *suggested* kind would be
   right, so the human label must not copy governance state blindly.
2. **retention_value** — retain / do_not_retain / uncertain per admission-v1.
   Retention is durable usefulness, never truth.
3. **epistemic_state** — the admission-v1 seven-value vocabulary at
   decision time only.
4. **consequence** — low / medium / high / unknown per admission-v1.
   Consequence of erroneous silent admission, never inferred from confidence
   or wording.
5. **reviewer confidence** — low / medium / high on your own judgment.
6. **acceptable_abstention** — whether "unknown/no score" would be an
   acceptable provider answer for this item.

## Dual review and adjudication

- Every sample whose final consequence is `high` requires two independent
  reviewers (Reviewer A + Reviewer B) before it counts as complete.
- Any substantive disagreement on a calibrated dimension (expected_kind,
  retention_value, epistemic_state, consequence) requires explicit operator
  adjudication with a recorded reason; majority-by-omission is forbidden.
- Pre-adjudication labels are preserved verbatim in protected evidence.
- Reviewer handles are opaque. No names, credentials, or private text.

## Calibration outcome mapping (frozen)

The fitting stage derives binary per-dimension outcomes from FINAL adjudicated
labels exactly as follows — reviewers do not apply this mapping themselves:

| Dimension | positive | negative | unknown (excluded from support) |
| --- | --- | --- | --- |
| taxonomy | expected_kind == provider suggested_kind | expected_kind differs | expected_kind or suggestion missing/`unknown` |
| retention | retention_value == retain | retention_value == do_not_retain | retention_value == uncertain |
| epistemic | epistemic_state in {adequately_supported, weakly_supported} | contradicted, contested, unverifiable | unknown, ambiguous |

Unknown is a legitimate, protected answer. Never guess to avoid it.

## Honors

- Do not lower a dimension to make agreement look better.
- Do not review with the goal of matching what Engram currently does.
- If the packet content is insufficient to judge, label `unknown` and note it.
