# Admission v1 field reference

Read this reference with [the labeling handbook](admission-v1.md).

All objects reject unlisted fields. All listed fields are required unless
the table marks them optional. Null and unknown are distinct values.

Reserved enum values have no synthetic sample coverage in v1. They remain
available for real-data adjudication. Contract tests exercise unknown,
dual review, unresolved disagreement, and resolution separately.

## LabelRecord

| Field | Required | Type / allowed values | Reserved |
| --- | --- | --- | --- |
| `sample_id` | yes | string | — |
| `label_schema_version` | yes | engram-admission-label-v1 | — |
| `dataset_id` | yes | string | — |
| `dataset_version` | yes | string | — |
| `source_sample_ref` | no | string or null | — |
| `content_hash` | no | string or null | — |
| `fixture_role` | yes | ordinary_claim, stale_claim, incorrect_claim, ambiguous_claim, contested_claim, conflict_peer, distractor, adversarial, non_propositional | — |
| `label_origin` | yes | synthetic_authored, human_adjudicated | human_adjudicated |
| `reviewer_a` | yes | HumanJudgment | — |
| `reviewer_b` | yes | HumanJudgment or null | — |
| `resolution` | yes | HumanJudgment or null | — |
| `disagreement` | yes | none, unresolved, resolved | resolved, unresolved |

## Dimensions

| Field | Required | Type / allowed values | Reserved |
| --- | --- | --- | --- |
| `atomic` | yes | yes, no, unknown | — |
| `proposition_count` | yes | zero, one, multiple, unknown | — |
| `attribution` | yes | adequate, inadequate, unknown, unavailable, not_applicable | inadequate, not_applicable, unavailable |
| `source_span` | yes | adequate, inadequate, unknown, unavailable, not_applicable | adequate, inadequate, not_applicable, unknown |
| `evidence_span` | yes | adequate, inadequate, unknown, unavailable, not_applicable | adequate, not_applicable, unknown |
| `assertion_origin` | yes | direct_user, agent_inference, unknown, unavailable | unavailable |
| `expected_kind` | yes | preference, fact, observation, decision, procedure, summary, doctrine, invariant, diary_entry, unknown | — |
| `expected_subject_or_domain` | yes | string | — |
| `expected_scope` | yes | private, workspace, tenant, unknown | tenant, unknown |
| `retention_value` | yes | retain, do_not_retain, uncertain | — |
| `epistemic_state` | yes | adequately_supported, weakly_supported, contradicted, contested, ambiguous, unverifiable, unknown | — |
| `factual_outcome` | yes | verified_correct, verified_incorrect, became_outdated, not_verifiable, not_yet_known or null | — |
| `consequence` | yes | low, medium, high, unknown | unknown |
| `expected_storage_disposition` | yes | retain, reject, defer, unknown | — |
| `expected_startup_eligibility` | yes | yes, no, unknown | yes |
| `expected_governed_semantic_eligibility` | yes | yes, no, unknown | yes |
| `human_review_required` | yes | yes, no, unknown | no |
| `acceptable_abstention` | yes | yes, no, unknown | unknown |
| `conflict_expected` | yes | yes, no, unknown | unknown |
| `dispute_expected` | yes | yes, no, unknown | unknown |
| `supersession_expected` | yes | yes, no, unknown | unknown |
| `temporal_validity_issue` | yes | yes, no, unknown | unknown |
| `scope_visibility_concern` | yes | yes, no, unknown | unknown |
| `evidence_independence` | yes | known_independent, known_shared_root, unknown | — |
| `expected_blockers` | yes | array of string or null | — |
| `expected_next_action` | yes | automatic_admission, review, wait, reject, unknown | reject, review |

## HumanJudgment

| Field | Required | Type / allowed values | Reserved |
| --- | --- | --- | --- |
| `adjudicator_ref` | yes | string | — |
| `adjudicated_at` | yes | date-time | — |
| `adjudicator_confidence` | yes | low, medium, high, unknown | high, low, unknown |
| `reason_code` | yes | string | — |
| `dimensions` | yes | Dimensions | — |
| `usefulness` | no | Usefulness or null | — |

## Usefulness

| Field | Required | Type / allowed values | Reserved |
| --- | --- | --- | --- |
| `task_ref` | yes | string | — |
| `context_ref` | yes | string | — |
| `useful` | yes | yes, no, unknown | no, unknown |

## Manifest

| Field | Required | Type / allowed values | Reserved |
| --- | --- | --- | --- |
| `manifest_schema_version` | yes | engram-eval-dataset-manifest-v1 | — |
| `dataset_id` | yes | string | — |
| `dataset_version` | yes | string | — |
| `label_schema_version` | yes | engram-admission-label-v1 | — |
| `created_at` | yes | date-time | — |
| `snapshot_as_of` | yes | date-time | — |
| `source_class` | yes | synthetic, dogfood, incident, sanitized | dogfood, incident, sanitized |
| `code_sha` | yes | string | — |
| `sample_count` | yes | integer | — |
| `eligible_population_count` | yes | integer | — |
| `allowed_use` | yes | evaluation_only | — |
| `privacy_class` | yes | public_synthetic, sanitized_fixture, private_dogfood, private_incident | private_dogfood, private_incident, sanitized_fixture |
| `sampling` | yes | Sampling | — |
| `sample_ids` | yes | array of string | — |
| `sample_content_hashes` | yes | array of string | — |
| `data_digest` | yes | string | — |
| `stratum_counts` | yes | array of array of tuple | — |

## Sampling

| Field | Required | Type / allowed values | Reserved |
| --- | --- | --- | --- |
| `selection_method` | yes | census, stratified_hash | stratified_hash |
| `selection_seed` | yes | string | — |
| `strata` | yes | array of source_type, kind, review_status, blocker, evidence_state, selected_lane, age_bucket, conflict, dispute, recalled, labeled_consequence | — |
| `per_stratum` | yes | integer | — |
| `excluded_strata` | no | array of string | — |
