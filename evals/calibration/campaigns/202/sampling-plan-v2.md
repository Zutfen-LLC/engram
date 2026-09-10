# #202 corrected pre-review sampling plan v2

Status: frozen before human labels. This plan supersedes the defective v1 pre-review freeze.
It does not authorize assessment selection or serving changes.

## Population and selection

The eligible population is the authorized dogfood snapshot after the committed inclusion and
exclusion rules. Selection is deterministic for the campaign ID, seed, snapshot, and code SHA.
Provider output, assessment success, candidate decisions, policy output, reviewer labels, and
reviewer agreement are not sampling inputs.

Selection first applies proportional deterministic allocation to the recorded tuple:

- memory kind;
- source type;
- review status.

It then unions deterministic marginal-coverage selections for each mechanically available
pre-provider axis:

- source type;
- memory kind;
- review status;
- deterministic age bucket: `<7d`, `7–29d`, `30–89d`, `>=90d`;
- input-size bucket: `<=256B`, `257–1024B`, `1025–4096B`, `>4096B`;
- assertion mode, origin, risk, and evidence state only when the frozen snapshot actually records
  them.

Rows are hash-ranked with an opaque sample-ID tie-breaker, making the result independent of input
order even when duplicate content hashes tie.

## Requested dimensions not mechanically sampleable from this snapshot

The manifest records each limitation instead of manufacturing coverage:

- assertion mode, origin, risk, and evidence state: unavailable when absent from every snapshot row;
  explicit recorded `unknown` remains distinct from absent `unavailable`;
- direct-user versus inferred/extracted: unavailable because principal/assertion provenance is not
  present, and source type is not a valid proxy;
- consequence and retention disposition: measured only after blind human review;
- ambiguous/contested state: unavailable because the snapshot has no decision-time contested field;
- easy/difficult: unavailable because no objective pre-provider difficulty field exists; input size
  is reported separately and is not called difficulty;
- provider success/failure/abstention: measured only after membership is frozen and therefore cannot
  influence selection.

No evidence independence, epistemic truth, consequence, directness, or origin is inferred from
principal, session, model, source-type, count, or text-similarity proxies. This campaign does not
implement #161.

## Split contract

The split is constructed from the exact frozen sampled frame, never the full eligible population.
Exact and normalized-text duplicate groups are unioned before assignment. Group assignment targets
the development fraction by item count while keeping every group in one split.

The protected validator fails closed unless:

- development and holdout are disjoint;
- their union equals the exact sampling-manifest membership;
- every sampled ID appears exactly once;
- no unsampled ID appears;
- sampling-manifest and membership digests match;
- duplicate groups do not cross the split.

## Artifact boundary

Exact membership, hashes, duplicate groups, sample content, and reviewer packets are protected data.
They are exclusively created under `0700` directories with `0600` files. Public output contains only
rules, aggregate population/selection counts, sanitized coverage categories, and digests.

Frozen before any labels exist:
300 total reviewed, 150 labeled per dimension, at least 50% non-unknown coverage per dimension,
100 holdout overall and at least 10 labeled holdout observations per emitted profile,
with calibrated Brier <= 0.25 and calibrated ECE <= 0.15 for every emitted profile stratum,
20 high-consequence reviewed, 50 fitted observations per used bin, and 10 reviewed/labeled
observations per claimed supported stratum. Every floor is evaluated from reviewer,
observation, and fitted-profile evidence. Holdout and high-consequence support require real non-unknown
labeled evidence; empty or all-unknown evidence cannot pass. Thin bins or strata and partially
unsupported dimensions stay explicitly uncalibrated.

Reviewer A and Reviewer B receive independently blinded full-population packets in the corrected
pre-review freeze. Requiring Reviewer B to label the full sample is a strict superset of the issue's
minimum dual-review set (all high-consequence and policy-disagreement cases), avoids deriving a queue
from model/policy outcomes, and is compatible with exact count/order ingestion. Reviewers must not see
each other's labels or provider/policy output before adjudication.
