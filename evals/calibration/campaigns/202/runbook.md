# ENG-CALIBRATION-001F (#202) operator runbook — corrected pre-review stage

Status: **STOPPED FOR HUMAN ADJUDICATION** (reviewed Round-2 re-freeze
complete; see below). No human calibration labels have been accepted at any
point. Do not fit, gate, enable selection, or claim the #202 terminal result
before the full human review and adjudication contract is complete.

## Round-2 re-freeze against the deployed runtime (2026-09-10)

The corrected v2 freeze was re-frozen after deploying merged `main`
(`25256f7615c27be683e09e604df1a3bb553ff061`, PR #203) to `engram01` through
the documented Compose path (backup → rebuild → `up -d` → `engram init-db`;
database was already at 44/44 migrations, no new migrations existed in the
delta). The freeze ran **inside the deployed service container** via
`python -m evals.calibration freeze-target --derive-provider-config-digest`,
so the config identity was derived by `engram.assessments.assessment_config_version()`
from the exact runtime provider settings — the same helper production
`current_contract()` uses.

| Round-2 re-frozen identity | Digest/count |
| --- | --- |
| Campaign tooling SHA | `25256f7615c27be683e09e604df1a3bb553ff061` |
| Snapshot | `cc172a6f7c2780784ef0ce5686772a72324d1398976fc117da637df2a88fbdb0` (unchanged, verified) |
| provider_config_digest | `sha256:8488c809d9d1ace29470ad85d57cbebb01b997e1a5730a86263c0e40a0384b45` |
| Deployed `current_contract().config_version` | `sha256:8488c809d9d1ace29470ad85d57cbebb01b997e1a5730a86263c0e40a0384b45` (exact-equal to frozen) |
| Target identity | `57fc03918d5335c2925e2e6402fcadc4a29f5ef138fba6e6d839e9d8ec292ef9` |
| Protected frame | `e5c0c60b5d80a3a715a73cd4596e4cc07db604008dc8452cfde671e97045eb08` (unchanged) |
| Sampling manifest | `ed2e0c80bfe0c30c39d5ad5bc5656b007617320484efae14c87fa51027d66b3d` |
| Split manifest | `a2a27ed4c0152bf2d9b6c318bbfcfd6e5e20944184a0cc9df18cb2b4e3fbb72b` |
| Reviewer A packet | `07f9fdbfdaae080fd860dc08f22cd56432826e115a017085004e92a0c9a7d5d4` |
| Reviewer B packet | `dbd952815222c232dd8a011c6972cb712963da77c1fedba04e712e57a2f82618` |

Counts are unchanged from the corrected v2 freeze, as required by the
membership invariant (sampling never consumes the target identity digest):

```text
eligible = 624
sampled  = 402
dev + holdout = 402  (241 + 161)
duplicate groups = 27, zero cross-split leakage
```

Membership, split assignment, frame digest, coverage, and strata are
byte-identical to the invalidated corrected v2 freeze; only the manifests'
recorded identity digests changed. An independent second regeneration from
the same deployed tooling, frozen target, snapshot, and seeds reproduced
every digest above exactly. The old bare-hex value (`725ee055…`) was not
reused or prefixed; the production helper independently derived the
canonical `sha256:<64hex>` form. No reviewer material, labels, or ledgers
existed before or after this re-freeze.

## Round-2 invalidation of the corrected v2 freeze

Two defects in the corrected v2 freeze made it unusable as frozen:

1. **Config identity representation.** `provider_config_digest` was frozen as a
   bare 64-hex digest. Production `current_contract()` emits
   `AssessmentContract.config_version` as `sha256:<64hex>`, and both the
   evidence verifier and production `calibrate()` compare it by exact equality.
   No real deployed contract could satisfy the frozen target, and normalizing
   only inside the verifier would let fitting proceed on an identity production
   would later reject as uncalibrated. Campaign identity now stores the exact
   production representation, and the previous bare-hex value
   (`725ee055…`) has no recorded capture provenance, so it must **not** be
   reused or simply prefixed.
2. **Tooling provenance conflated with deployed runtime identity.** The gate
   required `proof.deployed_repo_sha == deployed_repo_sha == target.repo_sha`.
   Once this branch merges, `main` necessarily carries a different SHA, so the
   gate could only pass by deploying the historical commit or re-freezing after
   every merge — even with the assessment contract completely unchanged.

Both corrections change `TargetIdentity`, and therefore the target identity
digest and every artifact transitively bound to it. The corrected v2 protected
bytes and their invalidation record are retained byte-for-byte in
`202-invalidated-corrected-v2-143ff3f`; its identity
(`b81348f7b6f14cf4eaf2ca735ae266babc410bb79299c247150bda054ab01614`), sampling
(`c899ab9fe836b3ad122a933e593d137fb79110dab6aed56984a02e36477c9f65`), split
(`0a97df59abe186e2b914a551bba802bf908253d10a53eddb2d82c7ab4e6c4611`), and packet
(`f37cb720…4990e` / `58353df8…8a4b27`) digests are audit history only.

No accepted reviewer result, frozen label ledger, or adjudication artifact
existed before this invalidation either.

### Campaign tooling SHA versus deployed runtime SHA

These are now two distinct identities and are never compared to each other:

```text
TargetIdentity.campaign_tooling_repo_sha  # revision that generated/froze the campaign
AuthoritativeRecallProof.deployed_repo_sha  # revision actually running on the probed host
```

The proof still binds to the runtime it probed (`proof.deployed_repo_sha ==
deployed_repo_sha`) for auditability. Runtime compatibility is gated where it
belongs: on the exact deployed `AssessmentContract` (provider, model, prompt,
schema, code, config, calibration version and digest) plus every serving
invariant. A newer runtime SHA is therefore never automatically trusted — any
contract drift on it still fails closed — while a merge commit that leaves the
calibrated contract identical no longer forces a re-freeze.

### Required operator re-freeze (completed 2026-09-10 — see above)

Run on the host whose runtime the campaign targets, so the config identity is
derived from real deployed provider settings rather than transcribed:

```bash
python -m evals.calibration freeze-target \
  --campaign-tooling-repo-sha "$(git rev-parse HEAD)" \
  --derive-provider-config-digest \
  --output "$XDG_DATA_HOME/engram/evals/202/identity-frozen.json"
```

`--derive-provider-config-digest` calls
`engram.assessments.assessment_config_version()` — the exact helper production
`current_contract()` uses. Passing `--provider-config-digest` with a bare 64-hex
value is refused outright. Then regenerate the transitively affected protected
artifacts (`sample`, then `packets`), recompute the target identity, sampling,
split, and packet digests, update this runbook and the public manifest, and
regenerate once more to prove deterministic equality.

Sampling and splitting never consume the target identity digest, so the
re-freeze changes recorded manifest digests only. Absent a snapshot change the
counts must be unchanged:

```text
eligible = 624
sampled  = 402
dev + holdout = 402  (241 + 161)
```

If any count moves, stop and explain why before proceeding.

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

| Corrected v2 identity (INVALIDATED round-2, audit history only) | Digest/count |
| --- | --- |
| Campaign tooling SHA | `143ff3ffa23cf3ab5884ce11d19c12621ade5aca` |
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
