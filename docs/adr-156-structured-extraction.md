# ADR: Structured extraction and provenance v1

Status: implemented behind an opt-in Hermes flag.
Issue: #156. Execution baseline: `7f66c810c9cd4c65458b16fbd18c9491e9c5e9e3`.

## Decision

`POST /v1/extract` accepts structured messages. It returns a versioned receipt
and zero or more atomic candidates. `GET /v1/extract/{run_id}` retrieves a
stored write-mode receipt. Both routes enforce authenticated scope. POST
requires `write`, including preview. GET requires `read`.

The wire schema is `engram.extraction.v1`. The prompt is `engram.extract.3`.
The checked-in JSON schema is `docs/schemas/extraction-v1.json`. The server
and SDK schemas must match. Tests enforce this condition.

Each candidate includes taxonomy and retention suggestions. It also includes
assertion mode, role, tool, evidence spans, explicit source cues, and a shared
evidence root. These fields do not provide risk, consequence, or admission
authority. Source cues do not assign a memory sensitivity classification. The schema rejects additional fields. When a provider emits no candidates,
a bounded unknown abstention code becomes `unsupported`. This does not
accept a candidate or create risk authority. Suggested kind is taxonomy
only. A `fact` can still require consequence assessment by #157.

## Attribution and evidence

Roles describe caller-supplied input. They do not authenticate transcript
speakers. `asserting_principal_id` remains null because this API cannot prove
speaker identity. The authenticated writer is recorded separately on the
receipt. First-person grammar does not identify a user.

The first evidence span identifies the assertion. Other spans provide context.
If a cited message starts with "it" or "they", the server also retains the
preceding message span as context. This does not prove which referent is correct.
Assistant output remains inference, derived summary, or quoted source. Tool
output remains a tool observation. System and unknown messages have unknown
assertion mode. Direct-user attribution does not change source authority.

Spans use zero-based Unicode character offsets. The end offset is exclusive.
Each span references an immutable message ID, input SHA-256, and character
count. A source cue includes its span and a bounded exact excerpt copied by
the server. The server resolves unique literal quotes when provider offsets
are incorrect. It rejects absent or ambiguous quotes. It also preserves
common literal cue patterns without assigning risk or authority. This preserves literal temporal, scope, security, negation, and
qualification cues for #157 without requiring a parse of memory text.

The evidence root hashes the tenant, workspace, and complete input message
manifest. All candidates from that batch share the root. Its basis is
`input_batch`. It proves grouping, not independent corroboration. Separate
batches can contain overlapping source material. Consumers must not interpret
different batch hashes as proof of source independence.

V1 accepts only `engram://items/{uuid}` source references. The source must be
readable and eligible in the exact destination workspace. Other URI schemes
are rejected. Engram does not fetch external content.

## Writes and retries

Preview creates no memory, ingest, or extraction receipt rows. Existing
optional usage telemetry can record bounded provider metrics. It does not
record raw input or provider error text.

`write_proposed` requires an idempotency key. A transaction advisory lock
serializes the tenant, principal, workspace, and key. Reusing that identity
with different input, visibility, or profile revision returns `409`.
Successful retries return the original receipt and original outcomes.

Each candidate uses a PostgreSQL savepoint. One candidate failure does not
remove other candidates. The final commit binds successful memory writes,
ingests, extraction links, and the complete receipt. A failed final commit
leaves no partial memory state. A retry can execute again after rollback.
A committed per-candidate error is terminal for that retry key. To retry a
corrected batch, use a new key. Existing successful content then deduplicates.
V1 does not offer caller-selected all-or-nothing batch semantics.

Candidates with `retain` and retention confidence of at least 0.65 can be
written. Other retention output recommends volatile storage. This threshold
controls storage only. Existing secret checks, governed kinds, write scopes,
source priors, and memory-profile restrictions still apply. Every new item
starts as proposed. A dedup result can reference a previously active item;
extraction does not change that item's review state.

Extraction taxonomy and retention output live in the extraction receipt.
They are not inserted as trusted `classification_runs` evidence and do not
set a memory item's retention confidence. Existing promotion policy remains
authoritative. Existing `/v1/classify` and `/v1/remember` behavior is preserved.

Receipts and links have FORCE RLS. They are principal-scoped and tenant-scoped.
Workspace membership also restricts receipt reads. API retrieval applies the
current profile. Database triggers reject receipt updates and invalid item
links. Receipt hashes use RFC 8785 canonical JSON and SHA-256. A hash attests
to the server-stored process result when checked against the stored receipt.
It is not a digital signature or proof of factual correctness.

## Bounds and privacy

V1 permits at most 64 messages and 65,536 request bytes. Each message permits
at most 16,000 characters. Output permits 32 candidates, 4,000 characters per
proposition, 16 evidence spans, and 16 source cues per candidate. A cue excerpt
permits at most 512 characters.

The server scans input content and metadata before provider submission. Any
secret match rejects the complete request. It scans output before storage.
Unsafe generated content is replaced with a fixed rejection marker.

The provider receives one bounded request. Provider retries are disabled.
The request has a 30-second client timeout, a 35-second outer timeout, and an
8,192-token output limit. The endpoint uses the configured classification
provider, model, base URL, and credential. Receipts retain the configured model,
returned provider model, tokens, latency, and provider-reported cost. Missing
cost remains null.

Raw transcripts are not stored in extraction tables or repeated in memory
rows. Receipts contain input hashes, metadata, normalized propositions, and
bounded cue excerpts. Operators must protect receipts as memory data.

## Hermes and rollback

`ENGRAM_HOOKS_STRUCTURED_EXTRACTION=false` is the default. When enabled,
lifecycle hooks send structured messages through the SDK. The local guard
and secret denylist run before submission and volatile storage. A rejected message blocks the batch to preserve
context integrity. Missing roles remain unknown. Missing IDs receive stable
hashes based on the message position, role, and content.

Successful new writes use the `written_proposed` result term. `promoted`
remains a compatibility counter for existing consumers. It does not mean
admission. A retried request returns the original outcome and receipt.

Provider or server failure stores the exact structured request and retry key
in the bounded local volatile store. The file uses mode `0600`.
Client errors from the server reject the batch instead of storing it locally. Low-retention candidates also go to that
store. A timeout reports no durable write because the commit may be unknown.
Replay the saved request with its original key to resolve that uncertainty.

To roll back capture, set the flag to false and reload Hermes. The existing
local extraction/classifier pipeline remains available. Before database
downgrade, disable structured capture and stop extraction requests. Back up
receipts if they must be retained. Apply
`migrations/downgrades/036_extraction_receipts.sql` with the migration role.
The downgrade removes extraction tables and triggers. It preserves all
memory items and production policy state. Reapplying migration 036 recreates
the empty extraction tables.

## Evaluation

`evals/extraction/golden-v1.json` contains 19 synthetic cases. They cover
preferences, corrections, rationale, uncertainty, negation, changed minds,
pronouns, tools, summaries, transient chatter, injection, privacy, duplicates,
paraphrases, unknown origin, abstention, secrets, and the #162D-shaped case.
The expected labels are evaluation inputs only. Runtime extraction does not
import the golden set or the #162D human consequence labels.

The evaluator uses frozen lexical criteria for proposition matching. It
reports candidate precision and recall, attribution, evidence validity and
coverage, cue coverage, kind, retention, duplicates, abstention, tokens,
latency, and reported cost. Valid paraphrases can fail lexical matching.
Review case outputs as well as aggregate scores. This small synthetic set
does not certify production admission or justify enabling the flag by default.
