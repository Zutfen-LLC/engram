# ENG-CALIBRATION-001K (#216) operator runbook — FIX3 pre-review stage

Status: AWAITING MAINTAINER RE-REVIEW.  Do not paste reviewer prompts, import
reviewer responses, execute HOLDOUT, fit external labels, merge PR #217, or
close #216.

## FIX3 execution identities

| Fact | Value |
| --- | --- |
| accepted base | `468cfc0a88119943fd02520c5fdb70a25715c1c8` |
| reviewed remote starting head | `22a4d767d8f3070cc99932748e2012893af1c259` |
| superseded functional SHA | `10f3f579e1893ee69480598511b5feb76ff3896d` |
| FIX3 functional execution SHA | `c1d35a9b0263deda4bcb71444619cb061ce2fc1a` |
| superseded target identity digest | `ca2736335917d768458457acb0db34594075a54fae295ebb7d99c857e705c5c6` |
| initial failed local-config target | `513e2ff15d907dca8c87909f325ff81bfdbb78905527c8d3e9dc2d87f4c375e4` |
| current target identity digest | `6cf3a3c8dd1c6d12c9ce78719cccbe449d850375fd59dbc8c052ae9dbf4aa66c` |
| current target file SHA-256 | `ce08d8003a61d7b8396b80dea267605d1a48c8fc33f594867f013360cd03d517` |
| provider adapter / model | `openai` / `deepseek-ai/DeepSeek-V4-Flash` at `api.deepinfra.com` |
| deployed provider config digest | `sha256:8488c809d9d1ace29470ad85d57cbebb01b997e1a5730a86263c0e40a0384b45` |
| target prompt / dimensions | `engram.assess.3` / `("taxonomy", "retention")` |
| schema / code contract | `engram.assessment.v1` / `assessment-engine-v1` |

The first FIX3 target used the clean checkout's local derived config digest
`sha256:24c8e17f...`, which the DEV executor rejected before any provider
request because deployed config is `sha256:8488c809...`. Its target and stale
DEV packet are preserved under
`protected/quarantined-fix3-invalid-local-config/`. The current target was
re-frozen from the clean functional checkout with the verified deployed config.

## Preserved superseded evidence

`protected/quarantined-fix3-superseded-c1d35a9/` preserves byte-for-byte:

- prior target bound to `10f3f579e1893ee69480598511b5feb76ff3896d`;
- prior target identity digest `ca2736335917d768458457acb0db34594075a54fae295ebb7d99c857e705c5c6`;
- prior DEV artifact `216-assess3-dev102-results.json`, SHA-256
  `c9c4d68b7ffd11157ab2196ad9b3b08d8573c517322ec08252f1ff9a92efc23c`;
- prior Stage-A lanes, batches, packets, and preparation artifacts.

Reason: execution-semantic campaign tooling changed after that target freeze.
Those bytes are historical evidence and are not relabeled as binding the FIX3
functional SHA. No HOLDOUT material is in this quarantine.

## Canonical reuse and split (reproduced unchanged)

A detached clean worktree at `c1d35a9...` regenerated these artifacts into an
empty authority directory. Their active and regenerated file SHA-256 values
matched exactly:

- reuse manifest `ba6ea5ab6f0a3ab43df6224777edeba96c971d7dc1e4765b08301eafcf6ac763`;
- split manifest `d5b4df72fb620f4e71728a401f959ac6b4793e545ba92a40b9cfe7355aa04e10`;
- reused labels `daee2af980ee46010674cc5105be327a85e9595e43b240cc0ea5d217493964cb`.

Logical digests: reuse `2a8d3e3f23dacef04fe742e14d7373abc33f3b6806d679453c64ee58d7385fd3`;
split `cb7434a5285536e540d9c8898ba446c35f25dfaaf22a1b3d8b323a40067bea37`;
reused labels `bc2218d870703ab64a0a25cef683fd19d0c19b29b801e3a07bb6dc6df817c40a`.
The partition remains reused DEV-200 + fresh DEV-102 (25 forced + 77 fresh) /
HOLDOUT-100, with zero cross-split duplicate groups.

## DEV provider evidence

Current protected artifact: `216-assess3-dev102-results.json`.

- SHA-256: `bb23bf7cbd538dc743af656dfb425804c6177d9b2298546959d83d6e164f7a14`.
- run kind: `issue-216-dev-102-assess3`.
- target: `6cf3a3c8...`; functional code head: `c1d35a9...`.
- status accounting: 102 `ok`, 0 `error`, 0 `abstained`.
- exact membership: `forced_dev_fresh ∪ dev_fresh`, 102 IDs; zero HOLDOUT IDs.
- sealed load through `ProviderEvidence216.load_verified()` reached
  `dev_fit_observations()` with 204 dimension observations across 102 distinct
  DEV samples. No HOLDOUT provider case was created or loaded.

## Stage A — DEV subscription authority and batches

- canonical stage: DEV, derived from target/reuse/split/membership; the
  sampling seed is metadata only;
- DEV sampling file SHA-256:
  `32f3cba54e63b84706aaf07ed85ea94f56a0691343842ed940d2eb0aa3df839a`;
- blind source packet SHA-256:
  `d09184d57dfe84ded15bfd812e3ada2e3757d77c451515b176a1e5afd2c74e88`;
- logical batch manifest SHA-256:
  `8f9f47ed9c5206957604dab1ef61ae33dacee2cc87c488771d624ba29d891ed7`;
- three lane authority digests:
  `fcbe0ba4c2570123d5b967232f6119ab8f41590b9ba23756c84c6f64f719ae2e`,
  `aa0ba77541e5be1032bac754455add6bebac3d07937fa33bbb96cc123f96ca3c`,
  `ddc19cd18cc60d1d9b3e45c8cc172d178a334214da7220d1db31dd11d6a0c128`;
- batches are 50 / 50 / 2; union == exact 102 DEV IDs and union ∩ HOLDOUT == 0;
- reviewer response artifact count: 0.

The next outstanding batch is not to be shown or pasted pending maintainer
review.

## Validation and serving invariants

Local FIX3 validation:

- `tests/test_calibration_216.py`: 79 passed;
- combined #206/#209/#216 regression matrix: 527 passed;
- assessment/calibration regressions: 103 passed;
- scoped ruff and repository-authoritative
  `mypy --explicit-package-bases evals/calibration/`: passed;
- full suite: 3101 passed, 1608 skipped; 18 failures and 53 errors were all
  PostgreSQL `localhost:5432` connection refusals, reproduced on reviewed head
  `22a4d76` with one portal failure and one doctor teardown error.

```text
assessment_selection_enabled == false
CERTIFIED_SERVING_PROFILES == {"legacy"}
```

- hosted CI for documentation/evidence head `1b673a4fbe341ada5f0e7445e735a5b8eba2bcf3`:
  `repository-safety` and `conformance-vectors` / `runtime-image-smoke` /
  `compose-real-db` (all four real-Postgres shards) run
  `34702450366`; `changed-python-format` run `34702450364`; all completed
  successfully.

HOLDOUT provider/reviewer/evaluation execution count is 0. The final
PR/documentation head is intentionally recorded in the maintainer handoff
rather than self-referentially inside this commit.

## FIX4 remote DEV-102 execution

- functional SHA / remote detached checkout: `4ae703e9219a7afd31f202c921e500b63537ce04`;
- target digest / target-file SHA-256: `49a4ea3a9fd406139fbc8447766e7e50c91236b9d11de1ed809a9290cff01ed1` /
  `09c72d60f139d108650f149dc4f4f4b2972b69d3e7ff175be10cca77997a6864`;
- remote provider executor: production `engram.assessment_provider.assess_content`, invoked once per canonical DEV input in an isolated `engram01` container inheriting deployed configuration; no secret material transferred;
- live provider: `openai` / `deepseek-ai/DeepSeek-V4-Flash` at `api.deepinfra.com`, credential present without disclosure; config digest
  `sha256:8488c809d9d1ace29470ad85d57cbebb01b997e1a5730a86263c0e40a0384b45`;
- transfer-manifest SHA-256: `2898d0af63aaaa1aa4a7e301572f8495625bba1ea393284472454738ef9576e0`;
- sealed provider result SHA-256: `b3272000d8d5baa7d43a270b8257987add2360d5b74893a8fe552653cd76b20a`;
  102 exact DEV cases, 102 ok / 0 error / 0 abstained, zero HOLDOUT calls;
- DEV sampling / blind source / neutral / neutral-manifest SHA-256:
  `0a13ae689842269d5ee7dbce970a36f53b3c0d045f8831266b361305b710bd69` /
  `d977d441b9534c8237e318e97d95e3ca4b5177c81f73aa7ec5615b2c20ac6458` /
  `0ebc67c8de87d4738d5bb702e9ae6a7a7d12ce3c2b0866131562abd8cf870428` /
  `28d4dc65e539c0542e3ab27a49d9f18839a59c9eabc1273eee9c3621ae2feebb`;
- lane authority digests (a/b/c):
  `17c863ab3f2c546eb2c57d30fac696c213ab7195c3e07bec87a2748d9cb9114c`,
  `575be4a55b2ff26e23fbf4d221bf42c859a64716e5fe2d6854e7121a0dad4599`,
  `67483807f78615212e893aa8c109940fdf2e4f2c03a82e2238a7dc54e71cf9ab`;
- preparation SHA-256: `245dfe5def1dadcfcd60252fc96a13b4e46cb1e842fd969ede3f04992eb88d86`;
  batch-manifest digest `b97ab0ab4832cceec92252a105c0cf7ad70fca93e5579761c5210bc9ec89632d`;
  deterministic batches 50 / 50 / 2, exact DEV union, zero HOLDOUT overlap;
- reviewer responses/imports: 0. HOLDOUT remains locked: freeze/unlock artifacts absent;
  no provider/reviewer/evaluation execution. Serving invariants unchanged.

STOP: reviewer batches remain unexecuted; no consensus, Phase-5 fit, candidate freeze, HOLDOUT unlock, or HOLDOUT evaluation occurred.
