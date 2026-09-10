# ENG-CALIBRATION-001F (#202) operator runbook — corrected pre-review stage

Status: **STOPPED FOR HUMAN ADJUDICATION**. The corrected v2 preparation
artifacts are frozen. No human calibration labels have been accepted. Do not
fit, gate, enable selection, or claim the #202 terminal result before the full
human review and adjudication contract is complete.

## Invalidated v1 pre-review freeze

The v1 freeze is invalid because it sampled 402 rows but split all 624 eligible
rows into 362 development and 262 holdout cases. The split and every downstream
artifact depending on it are superseded. The original protected directory was
retained byte-for-byte with a protected invalidation record; it was not
rewritten or remapped.

No accepted reviewer result, frozen label ledger, or adjudication artifact
existed before invalidation. Both old 402-row templates had empty response
fields.

| Invalid v1 identity | Digest/count |
| --- | --- |
| Tooling repository SHA | `3d17923e6553a5a633a819682f9ffad8423cd8ef` |
| Target identity | `78d45df3b89261bc95adcf4fdaf4eeb3b54784c5dc4d5cea1fbd85a57b8f3093` |
| Sampling manifest | `27de308acfac24f4a9292cf93d022f333277f1a080b529cbe74e665f64ec8475` |
| Split manifest | `2f2b7ec5dccb0ab4882fa66906b5e9e4136c54632b0d44ef06fc3ec49ffeed73` |
| Reviewer A packet | `aae8e0bd84642fde28ced42c404cb1b32b7f9fd7323605c7cda6852ddfd7443f` |
| Reviewer B packet | `d6dc1bb20372c7097d44be024ed03fdaad10174136ec4d548abb0f175ae3a021` |

A preliminary v2 regeneration at tooling SHA
`66b11a388bb4e7a2060efafa191df35b1f698cd7` was also superseded before review
when final provenance, calibrated-holdout, and canonical content-hash hardening
changed the tooling. Its protected bytes and invalidation record are retained;
its identity (`ea8d5403f2ad0b0b93f1553fd7cfd7093de765104826d9b9af4a44e186ebbd4e`),
sampling (`449197e8e19d25aba2d936e94bdb6f669890735cd74ee8d9d3e481e575c2e5e3`),
and split (`dfd71510a58f4c35a4feed6a130351362521e43f380981ff0ea45be8fec5414b`)
digests are audit history only.

A preliminary v3 regeneration at tooling SHA
`fd3ddf9c7429712af6c47680267742b421e43fbe` (identity
`d36bc930d09ec906f17ff7a46bda59cb34297244ef7d2f820e0a8ae422aa748d`, sampling
`533ee1e338c1985c943ef186d2bb2273740fc4dd61a43d20ca11d1031f288dfa`, split
`ad248d6605c98b8c4a544d43219576af9a3058c813a99a27c5a082aa18ce79f6`, packets
`9c5ef5e4…6920d` / `bc384a50…9c36c`) was likewise superseded before review when
the final evidence-binding and grouping corrections were committed. Its
protected bytes and invalidation record are retained byte-for-byte in
`202-invalidated-preliminary-v3-fd3ddf9`.

## Corrected v2 freeze

The unchanged authorized snapshot was copied byte-for-byte into a new protected
root at `$XDG_DATA_HOME/engram/evals/202` (resolved from the operator account's
XDG data directory). Protected directories are mode `0700`; files are exclusively
created at mode `0600`. Exact membership, content, duplicate groups, packets,
reviewer material, ledgers, and assessment evidence remain outside Git. Keep both
the invalidated and replacement trees through review, adjudication, and audit
sign-off. Nothing expires automatically; later archival or destruction requires
explicit data-owner authorization while retaining this content-free public digest
and invalidation trail.

| Corrected identity | Digest/count |
| --- | --- |
| Corrected tooling SHA | `143ff3ffa23cf3ab5884ce11d19c12621ade5aca` |
| Snapshot | `cc172a6f7c2780784ef0ce5686772a72324d1398976fc117da637df2a88fbdb0` |
| Target identity | `b81348f7b6f14cf4eaf2ca735ae266babc410bb79299c247150bda054ab01614` |
| Sampling manifest | `c899ab9fe836b3ad122a933e593d137fb79110dab6aed56984a02e36477c9f65` |
| Split manifest | `0a97df59abe186e2b914a551bba802bf908253d10a53eddb2d82c7ab4e6c4611` |
| Reviewer A packet | `f37cb72088aaa2509fdf8e974b8722097a46f8886cadd68308e996502914990e` |
| Reviewer B packet | `58353df8684dfbf3c6659129cf90a3e4f04d590717c3d92b7451eb9b748a4b27` |

Corrected counts:

- eligible: 624
- sampled: 402
- development: 241
- holdout: 161
- duplicate groups: 27, all contained within one split

The public and protected invariants are:

```text
402 == 241 + 161
dev ∪ holdout == exact sampled membership
dev ∩ holdout == ∅
```

## Frozen sampling and coverage

The methodology is frozen in `sampling-plan-v2.md`. Membership is deterministic
for the snapshot, campaign identity, v2 seeds, and corrected tooling SHA. It
does not consume provider output, policy results, reviewer labels, or reviewer
agreement.

Mechanically sampled axes available in the snapshot:

- source type;
- memory kind;
- review status (`active` and `proposed`);
- deterministic age bucket;
- objective input-size bucket, reported as size rather than “difficulty.”

Explicitly unavailable from the snapshot:

- assertion mode and assertion origin;
- direct-user versus inferred/extracted provenance;
- risk/consequence state;
- evidence/epistemic state;
- ambiguous/contested state;
- an objective easy/difficult classification.

Retention disposition and consequence are post-review-only. Provider
success/failure/abstention is post-selection-only and cannot influence sample
membership. `unknown` remains distinct from absent `unavailable`. No evidence
independence, consequence, epistemic truth, or origin is inferred from source,
principal/session/model counts, or content similarity. This campaign does not
implement #161.

## Frozen evidence floors

- 300 completed reviewed samples;
- 150 non-unknown labels per calibrated dimension;
- at least 50% non-unknown coverage per calibrated dimension;
- 100 reviewed holdout samples, at least 10 calibrated holdout observations per
  emitted profile, calibrated Brier no greater than 0.25, and calibrated ECE no
  greater than 0.15;
- 20 completed high-consequence reviews plus non-unknown development support in
  every calibrated dimension;
- 50 fitted observations per used reliability bin;
- 10 reviewed/labeled observations per claimed supported stratum;
- independent full-population dual review for every frozen sample.

Every floor is evidence-derived. Empty/all-unknown holdout or high-consequence
evidence cannot pass. Thin/unprofiled strata and unused bins remain explicitly
unsupported; one dimension’s support cannot hide another dimension’s failure.

## Human review stop point

1. Reviewer A independently labels all 402 corrected v2 cases under
   `engram-calibration-guide-157-v1`.
2. Reviewer B independently labels all 402 corrected v2 cases from the separate
   full-population packet. This strict superset necessarily dual-reviews every
   high-consequence and policy-disagreement case without selecting membership
   from model or policy output.
3. Reviewers must not see provider scores, model suggestions, policy decisions,
   or each other’s labels before adjudication.
4. The operator adjudicates every substantive disagreement with a recorded
   reason. Unresolved samples contribute no calibration support.
5. Ingestion must match exact packet count, unique IDs, order, and dataset
   identity before a protected ledger can be frozen.

## Post-review library workflow — blocked now

After real labels are frozen, and not before:

1. Capture exactly one provider execution receipt for every frozen sample under the
   frozen target contract. Each content-free receipt binds a unique execution ID,
   frozen input-content hash, mechanically derived request digest, provider-response
   digest, all three raw `AssessmentDimensions`, and its own payload digest. Store the
   complete receipt population in a protected assessment-evidence envelope bound to
   target identity, the exact target-compatible contract, protected-frame digest,
   sampling-manifest digest, and an independently retained file SHA-256.
2. Verify the protected ledger bytes against an independently retained SHA-256,
   both packet-file SHA-256 values, exact sampling membership/content hashes, and
   dataset identity. `check_floors` rejects incomplete receipt populations, reused
   execution IDs, target/contract drift, frame substitution, and malformed receipts;
   it derives all three observations per sample from the verified execution output,
   protected frame, frozen split, and adjudicated ledger. Caller-supplied outcomes,
   consequence, and strata are not authority and cannot manufacture support.
   Insufficient or partial evidence stops without lowering any threshold.
3. Reject duplicate profile keys. Fit development observations only, then evaluate
   the untouched holdout; unsupported bins reduce calibrated support/coverage and
   cannot borrow raw observations.
4. Capture fresh (maximum 24 hours), bounded HTTP `/v1/recall` and MCP probe
   evidence bound to the target, profile set, deployed contract, and policy.
5. Freeze that probe's SHA-256 inside the immutable calibration artifact, then
   build and load the artifact. The gate accepts no caller-supplied replacement
   digest.
6. Run production `calibrate()` mismatch proofs for provider, model, prompt,
   schema, code, config, calibration version/digest, and profile-stratum fields.
7. Missing, malformed, stale, failed, or mismatched recall evidence returns
   `KEEP_DISABLED`.
8. The gate may recommend only dogfood shadow assessment selection. It never
   mutates configuration.

The operator-facing CLI intentionally provides only `freeze-target`, `sample`,
and `packets` at this pre-review stage. Ingestion, fitting, gating, and reporting
remain library-only until reviewed evidence exists.

## Serving invariants

- `assessment_selection_enabled == false`
- `CERTIFIED_SERVING_PROFILES == {"legacy"}`
- ordinary `/v1/recall` remains legacy-authoritative
- MCP recall remains legacy-authoritative
- no serving default, recall score, ranking weight, lifecycle state, or candidate
  authority changed
