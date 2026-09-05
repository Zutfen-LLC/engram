# Admission labeling handbook v1

This handbook defines `engram-admission-label-v1` and
`engram-eval-dataset-manifest-v1`. The machine schemas are in `evals/schema/`.
The generated field reference below lists every field and closed vocabulary.
Pydantic validation also enforces identity, hashes, and reviewer relationships.
JSON Schema alone does not enforce these relationships.

## Purpose and authority

Reviewers must not label an item as correct merely because Engram currently promoted it.

Reviewers must not label an item as incorrect merely because Engram currently blocked it.

Labels express judgments. Policy inputs describe recorded system state. Policy
results describe observed behavior. None of these evaluation artifacts can
supply production evidence. Do not import this package into production code.

`synthetic_authored` identifies test expectations written for this contract.
Its reviewer handles identify fixture authors, not completed human review.
`human_adjudicated` identifies actual human judgments. Use opaque reviewer
handles. Do not put names, credentials, or private text in handles or reason codes.

## Decision rules

Judge retention value as durability or usefulness for memory. A durable but
unsupported procedure can receive `retain` and `weakly_supported`. This is not
permission to admit it automatically. A correct greeting can receive
`do_not_retain`. Retention is not factual truth.

Judge epistemic state using evidence available at the decision time only.
`adequately_supported` means that attributable evidence supports the claim.
`weakly_supported` means that support exists but is insufficient.
`contradicted` means that available evidence opposes the claim.
`contested` means that relevant evidence or reviewers support incompatible claims.
`ambiguous` means that the proposition permits materially different interpretations.
`unverifiable` means that no feasible verification method applies.
`unknown` means that the available record does not support a judgment.

Record later factual verification only in `factual_outcome`.
`verified_correct` and `verified_incorrect` require an actual verification basis.
`became_outdated` means that a previously valid claim ceased to apply.
`not_verifiable` means that factual verification does not apply or is infeasible.
`not_yet_known` means that verification remains pending. Null means no outcome
judgment exists. Never use later verification to rewrite decision-time support.

Judge consequence as the effect of erroneous silent admission.
`low` means a small, reversible inconvenience. `medium` means material wasted
work or an incorrect operational decision with a feasible recovery.
`high` includes security invariants, organizational doctrine, destructive
instructions, and claims that can cause broad or irreversible damage.
`unknown` means that the consequence cannot be assessed from the context.
Do not infer consequence from confidence, wording, or memory kind.
A harmless test procedure can be low consequence. A deletion procedure can be high.

Judge each expected admission field independently. Storage `retain` means keep
the candidate; `reject` means do not store it; `defer` means wait for clarification
or evidence. Startup eligibility concerns startup use. Governed semantic
eligibility concerns permitted semantic retrieval under the applicable scope.
A retained proposal can remain ineligible for startup. Human review and acceptable
abstention are independent judgments. `unknown` is not equivalent to `no`.
The baseline measures promotion readiness only. It does not simulate initial
capture, scope enforcement, secret rejection, or retrieval ranking.

## Proposition, provenance, and governance

`atomic=yes` means that one durable proposition can be evaluated independently.
For multiple propositions, use `proposition_count=multiple` and usually
`atomic=no`. Keep the source sample intact. Escalate splitting to the corpus
curator. Do not invent subclaims or evidence spans.

Quality `adequate` means sufficient for this judgment. `inadequate` means a
known defect. `unknown` means that available information is inconclusive.
`unavailable` means that the source did not record the information.
`not_applicable` means that the dimension does not apply.
Attribution concerns the claimed speaker. Source span concerns the candidate's
source text. Evidence span concerns supporting material. Missing extraction
provenance stays unavailable or unknown until #156 supplies it.

Expected kind uses the listed built-in taxonomy. Use `unknown` for a custom or
unresolved kind. Subject/domain is a controlled opaque domain code; use the
literal `unknown` if necessary. Scope identifies the maximum intended audience.
It is independent of retrieval relevance.

A fixture role states why the claim exists in the dataset:

| Role | Meaning |
| --- | --- |
| ordinary_claim | Normal candidate proposition. This role does not establish truth. |
| stale_claim | Candidate whose temporal validity requires attention. |
| incorrect_claim | Deliberately incorrect candidate. |
| ambiguous_claim | Candidate with unresolved meaning. |
| contested_claim | Candidate with incompatible support or judgments. |
| conflict_peer | Candidate included to test a conflict relationship. |
| distractor | Candidate irrelevant to the evaluation context. |
| adversarial | Candidate intended to violate the admission contract. |
| non_propositional | Greeting, fragment, or other non-claim. |

For changed preferences, record temporal and supersession judgments independently.
For conflict, distinguish incompatible claims from a dispute about one claim.
Supersession concerns a replacement relation. Temporal validity concerns when a
claim applies. Scope/visibility concerns its audience. None establishes relevance.

`known_independent` requires evidence of separate origins.
`known_shared_root` means that reports derive from the same origin.
Different principal IDs do not prove independence. Use `unknown` when roots are
unrecorded. Three agents repeating one note are one known shared root.

Usefulness requires both a task reference and a context reference.
It is not factual or epistemic evidence. Do not accept a standalone `useful=true`.

`expected_blockers=null` means no blocker judgment exists. An empty list means
that no blocker is expected. Use canonical production blocker codes.
`expected_next_action` records an authored action judgment. `unknown` means no
responsible judgment is available. The runner can observe automatic readiness
and cooling. It does not infer a review requirement from every terminal blocker.

## Review and escalation

Record the reviewer handle, aware timestamp, confidence, and reason code for
each judgment. Confidence is the reviewer's confidence in the judgment, not the
system's confidence in the claim. `low`, `medium`, and `high` mean tentative,
reasonably supported, and well-supported judgments. `unknown` means unassessed.
Keep sensitive explanatory material in the protected review system referenced
by the opaque reason code.

Human high-consequence samples require two distinct reviewers. If reviewers
differ on any dimension or contextual usefulness, set `unresolved`. Exclude that
sample from correctness metrics until resolution. A final resolution must record
its own reviewer metadata and all dimensions. Keep both original judgments.
Set `resolved` only when that resolution exists. Escalate unresolved evidence,
consequence, scope, or interpretation to a curator. Do not resolve by copying
Engram's output. Reviewers should work without the policy result visible.

Abstention is appropriate when the evidence, interpretation, or scope cannot
support a responsible decision. Blocking promotion is not automatically an
abstention: it can mean cooling, disabled policy, or a kind restriction.

## Dataset identity, sampling, and privacy

Use stable dataset and sample IDs. Increment dataset version for any material
change. Source references are optional opaque handles. Portable datasets never
require production memory IDs. Public synthetic content hashes use SHA-256 of
the RFC 8785 serialization of the content string. Private captures preserve the
stored production content hash and omit content. These hash domains are distinct.

The data digest covers ordered samples, complete labels, all policy inputs,
configuration, and evaluation time. The manifest digest covers the full manifest,
including sampling configuration and the data digest. Object key order does not
matter. Sample order does matter. Digests are unkeyed integrity evidence, not
proof of authorization. Do not publish per-item private hashes without a privacy
review; low-entropy text hashes can permit guessing.

The snapshot population is all live proposed items visible to the authorized
principal in one tenant. It includes superseded proposals and all blockers.
Do not describe this as all tenant memories. RLS can restrict visible population.
The snapshot uses one PostgreSQL repeatable-read, read-only transaction.
Its database timestamp is also the explicit policy evaluation time.

Census selects the entire population except explicit exclusions. Stratified
sampling groups by the configured fields and selects the lowest seeded hashes
within each group. Input order does not affect selection. Stratum keys are
opaque SHA-256 values. Counts describe the full input population before
exclusions. Excluded stratum keys remain in the sampling configuration.
Risk is available only after a label exists; unknown labeled risk stays unknown.
Recalled state remains `unknown` when no usage data was captured. Age buckets
are sampling categories and do not implement promotion cooling policy.

`public_synthetic` is authored, nonprivate content. `sanitized_fixture` is reviewed
sanitized material. `private_dogfood` and `private_incident` must remain in a
protected location. Ordinary CI uses only public synthetic data. The CLI emits
only aggregates. Unknown source/kind categories are reported as unknown.
Private error output is a fixed reason code. Do not print validation exceptions.
Public dataset validation applies the production secret-pattern denylist. This
is a detection layer, not proof that arbitrary text contains no secrets. Review
public fixtures before publication. Secret cases use a redacted marker; tests
generate a fake matching token in memory and prove rejection.

The capture file omits memory text and raw item IDs. IDs are HMAC-SHA-256 values
using an operator-held key. Store this key separately to reproduce identity in a
future snapshot. Do not put connection strings, credentials, or the HMAC key in
a manifest. Raw review content, if needed later, remains in an authorized review
system. This slice does not export it.

Malformed numeric or structural inputs abort capture with a fixed reason code.
Missing evidence remains `none`; inconsistent recorded evidence is
`malformed/stale`. Missing configuration yields unknown decisions. None of these
states is converted to safe, zero confidence, or a successful admission.

## Complete field reference

The [generated field reference](field-reference-v1.md) lists required fields,
closed enums, nested types, and values reserved for later dataset coverage.
Regenerate it with `python -m evals.admission.reference` after changing a schema.
