"""Deterministic memory E2E audit harness — shared typed logic.

This module holds the framework-independent, testable core of the audit
harness described in ``ENG-AUDIT-001``:

* the stable reason-code and stage-status vocabularies;
* run-state serialization (resumable, secret-free, stored outside the repo);
* secret redaction for any operator/exception evidence that may leak secrets;
* report assembly that conforms to
  ``schemas/memory-e2e-audit-v1.schema.json``;
* schema validation against that JSON Schema.

The HTTP client and CLI live in ``scripts/run_memory_e2e_audit.py``. The
real-PostgreSQL deterministic promotion / access-control proofs live in
``tests/test_memory_e2e_audit_postgres.py``. Nothing here performs a network
or database call, so the unit tests exercise it without a live service.

Design constraints (from the spec):

* A failure in one stage must NOT prevent other stages from being tested with
  separately controlled fixtures — hence stages are independently resumable.
* The report must never contain secrets, raw environment dumps, or raw recall
  packets beyond the audit markers.
* A negative authorization result (expected denial) is ``pass_expected_denial``,
  not a failure.
* A low-confidence / non-retain classifier result is a ``finding``, not a
  failure.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "ReasonCode",
    "StageStatus",
    "STAGE_ORDER",
    "StageEvidence",
    "FixtureEvidence",
    "RunState",
    "AuditReport",
    "sanitize_host",
    "redact_secrets",
    "validate_report",
    "load_schema",
    "finalize_report",
    "reason_code_from_exception",
    "is_failure_status",
    "STAGE_LABELS",
]

SCHEMA_NAME = "engram.memory-e2e-audit"
SCHEMA_VERSION = "1.0"

# Canonical, immutable stage ordering. Each stage is independently resumable.
STAGE_ORDER: tuple[str, ...] = (
    "stage_0_identity_preflight",
    "stage_1_hermes_write",
    "stage_2_processing_promotion",
    "stage_3_recall_fixture",
    "stage_4_access_recall_preflight",
    "stage_5_hermes_recall",
    "stage_6_epistemic_safety",
    "stage_7_negative_controls",
)

STAGE_LABELS: dict[str, str] = {
    "stage_0_identity_preflight": "Identity and environment preflight",
    "stage_1_hermes_write": "Hermes write interception",
    "stage_2_processing_promotion": "Processing and promotion observation",
    "stage_3_recall_fixture": "Controlled recall fixture creation",
    "stage_4_access_recall_preflight": "Direct access and recall-engine preflight",
    "stage_5_hermes_recall": "Fresh stock-Hermes recall",
    "stage_6_epistemic_safety": "Epistemic-safety fixture",
    "stage_7_negative_controls": "Negative access controls",
}

# Reason codes are deliberately a fixed vocabulary: every boundary reports a
# stable categorical reason so an ambiguous "the agent did not recall the
# memory" can never collapse distinct outcomes.
ReasonCode = str

StageStatus = str  # one of the literal statuses below

_FAILURE_STATUSES = frozenset({"failed"})
# blocked means an upstream stage did not complete, so this stage could not run.
# finding is a meaningful calibration result (low confidence, non-retain, etc.).
# not_run means the operator never invoked it.
# pass_expected_denial is a passing governance result (expected 403/404).
_BLOCKED_OR_FAILED = frozenset({"failed", "blocked"})
_PASSING_STATUSES = frozenset({"pass", "pass_expected_denial"})


def is_failure_status(status: str) -> bool:
    """True only for a genuine failure (not blocked, not a finding, not pass_expected_denial)."""
    return status in _FAILURE_STATUSES


# ── Secret redaction ─────────────────────────────────────────────────────────

# Engram API keys: eng_<key_id>_<secret> or legacy eng_<secret>. Match
# conservatively (the whole token after the prefix) so a truncated fragment
# never leaks.
_API_KEY_RE = re.compile(r"eng_[A-Za-z0-9_-]{6,}")
# Bearer tokens in Authorization headers / copied strings.
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{8,}")
# Postgres DSNs (password-bearing). Captures scheme://user:password@...
_PG_DSN_RE = re.compile(
    r"postgres(?:ql)?(?:\+asyncpg)?://[^\s:/@]+:([^\s/@]+)@",
)
# Generic key=... assignments commonly seen in dumps.
_KV_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|pwd|auth|bearer)\s*[:=]\s*"
    r"(['\"]?[A-Za-z0-9._\-+/=]{6,}['\"]?)"
)
_REDACTED = "***REDACTED***"


def redact_secrets(text: str) -> str:
    """Strip likely secrets from a free-form string.

    Used on any operator/exception evidence before it is stored in run state.
    Conservative and best-effort: the harness additionally never *collects*
    secrets in the first place (it reads credentials from env vars and never
    echoes them). This is the defense-in-depth backstop for content the
    operator pastes in (e.g. a Hermes response that happened to include an
    environment dump).
    """
    if not text:
        return text
    out = _API_KEY_RE.sub("eng_***REDACTED***", text)
    out = _BEARER_RE.sub("Bearer ***REDACTED***", out)
    out = _PG_DSN_RE.sub(
        lambda m: m.string[m.start() : m.start(1)] + "***REDACTED***" + "@",
        out,
    )
    out = _KV_SECRET_RE.sub(r"\1=***REDACTED***", out)
    return out


def sanitize_host(base_url: str) -> str:
    """Return only the host of a base URL (no scheme, path, query, credentials)."""
    # Cheap host extraction without urllib (base URLs are operator-supplied).
    s = base_url.strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    # drop userinfo
    if "@" in s:
        s = s.split("@", 1)[1]
    # drop path/query/fragment
    for sep in ("/", "?", "#"):
        if sep in s:
            s = s.split(sep, 1)[0]
    # drop port? Keep it — host:port is safe and useful, no credentials.
    return s


# ── Typed evidence containers ────────────────────────────────────────────────


@dataclass
class StageEvidence:
    """Per-stage evidence assembled into the report.

    ``evidence`` is a free-form dict but must contain only sanitized values
    (UUIDs, controlled statuses, counts, timestamps, bounded snippets). The
    CLI applies :func:`redact_secrets` to any operator-pasted strings.
    """

    status: StageStatus = "not_run"
    reason_code: ReasonCode | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "evidence": _sanitize_evidence(self.evidence),
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StageEvidence:
        return cls(
            status=d.get("status", "not_run"),
            reason_code=d.get("reason_code"),
            started_at=_from_iso(d.get("started_at")),
            completed_at=_from_iso(d.get("completed_at")),
            evidence=dict(d.get("evidence") or {}),
            limitations=list(d.get("limitations") or []),
        )


@dataclass
class FixtureEvidence:
    """One audit fixture (W / R / E) recorded in the report."""

    marker: str | None = None
    item_id: str | None = None
    created_by_role: str | None = None
    activation_method: str | None = None
    review_status: str | None = None
    visibility: str | None = None
    classification_run_id: str | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "item_id": self.item_id,
            "created_by_role": self.created_by_role,
            "activation_method": self.activation_method,
            "review_status": self.review_status,
            "visibility": self.visibility,
            "classification_run_id": self.classification_run_id,
            "created_at": _iso(self.created_at),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FixtureEvidence:
        return cls(
            marker=d.get("marker"),
            item_id=d.get("item_id"),
            created_by_role=d.get("created_by_role"),
            activation_method=d.get("activation_method"),
            review_status=d.get("review_status"),
            visibility=d.get("visibility"),
            classification_run_id=d.get("classification_run_id"),
            created_at=_from_iso(d.get("created_at")),
        )


# ── Run state (resumable, secret-free, stored outside the repo) ───────────────


@dataclass
class RunState:
    """Resumable per-run state persisted to ``<out>/<run_id>/state.json``.

    Holds the immutable run id, per-stage evidence, fixture ids, and an
    operator evidence scratch area. It must NEVER contain API keys, auth
    headers, database URLs, or raw environment — only ids, statuses, and
    sanitized snippets. :func:`RunState.to_json` asserts this on write.
    """

    run_id: str
    started_at: datetime
    target_host: str
    engram_revision: str | None = None
    hermes_revision: str | None = None
    tenant_acknowledged: bool = False
    identity: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, StageEvidence] = field(default_factory=dict)
    negative_controls: dict[str, StageEvidence] = field(default_factory=dict)
    fixtures: dict[str, FixtureEvidence] = field(
        default_factory=lambda: {
            "write": FixtureEvidence(),
            "recall": FixtureEvidence(),
            "epistemic": FixtureEvidence(),
        }
    )
    operator_evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, *, base_url: str, out_dir: Path) -> RunState:
        run_id = str(uuid.uuid4())
        state = cls(
            run_id=run_id,
            started_at=datetime.now(UTC),
            target_host=sanitize_host(base_url),
        )
        # A report is never sparse: every canonical boundary is represented
        # from the first state write onward.
        for stage in STAGE_ORDER:
            state.stage(stage)
        return state

    def fixture(self, key: str) -> FixtureEvidence:
        return self.fixtures.setdefault(key, FixtureEvidence())

    def stage(self, name: str) -> StageEvidence:
        return self.stages.setdefault(name, StageEvidence())

    def negative(self, name: str) -> StageEvidence:
        return self.negative_controls.setdefault(name, StageEvidence())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": _iso(self.started_at),
            "target_host": self.target_host,
            "engram_revision": self.engram_revision,
            "hermes_revision": self.hermes_revision,
            "tenant_acknowledged": self.tenant_acknowledged,
            "identity": _sanitize_evidence(self.identity),
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "negative_controls": {k: v.to_dict() for k, v in self.negative_controls.items()},
            "fixtures": {k: v.to_dict() for k, v in self.fixtures.items()},
            "operator_evidence": _sanitize_evidence(self.operator_evidence),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunState:
        return cls(
            run_id=d["run_id"],
            started_at=_from_iso(d["started_at"]) or datetime.now(UTC),
            target_host=d.get("target_host", ""),
            engram_revision=d.get("engram_revision"),
            hermes_revision=d.get("hermes_revision"),
            tenant_acknowledged=bool(d.get("tenant_acknowledged", False)),
            identity=dict(d.get("identity") or {}),
            stages={k: StageEvidence.from_dict(v) for k, v in (d.get("stages") or {}).items()},
            negative_controls={
                k: StageEvidence.from_dict(v) for k, v in (d.get("negative_controls") or {}).items()
            },
            fixtures={
                k: FixtureEvidence.from_dict(v) for k, v in (d.get("fixtures") or {}).items()
            },
            operator_evidence=dict(d.get("operator_evidence") or {}),
        )

    def to_json(self) -> str:
        rendered = json.dumps(self.to_dict(), indent=2, sort_keys=True, default=_json_default)
        # Defense-in-depth: the on-disk state must never carry a secret.
        assert_no_secrets(rendered, context="run state")
        return rendered

    @classmethod
    def from_json(cls, text: str) -> RunState:
        return cls.from_dict(json.loads(text))


def save_state(state: RunState, out_dir: Path) -> Path:
    """Atomically persist secret-free state to ``<out_dir>/<run_id>/state.json``."""
    run_dir = out_dir / state.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.json"
    rendered = state.to_json()
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=run_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            with contextlib.suppress(OSError):
                os.fchmod(fh.fileno(), 0o600)
            fh.write(rendered)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(temporary, state_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return state_path


def load_state(run_dir: Path) -> RunState:
    """Load run state previously written by :func:`save_state`."""
    return RunState.from_json((run_dir / "state.json").read_text(encoding="utf-8"))


# ── Report assembly + validation ─────────────────────────────────────────────


@dataclass
class AuditReport:
    """The final sanitized report conforming to the v1 schema."""

    run_id: str
    started_at: datetime
    completed_at: datetime | None
    target_host: str
    engram_revision: str | None
    hermes_revision: str | None
    identity_preflight: StageEvidence
    fixtures: dict[str, FixtureEvidence]
    stages: dict[str, StageEvidence]
    negative_controls: dict[str, StageEvidence]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "target": {
                "base_url_host": self.target_host,
                "engram_revision": self.engram_revision,
                "hermes_revision": self.hermes_revision,
            },
            "identity_preflight": self.identity_preflight.to_dict(),
            "fixtures": {k: v.to_dict() for k, v in self.fixtures.items()},
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "negative_controls": {k: v.to_dict() for k, v in self.negative_controls.items()},
            "overall": _overall(self.stages, self.negative_controls),
        }


def finalize_report(state: RunState) -> AuditReport:
    """Assemble the final report from run state."""
    stages = {name: state.stage(name) for name in STAGE_ORDER}
    # An incomplete report is an observation, not a completed audit.
    complete = all(ev.status != "not_run" and ev.completed_at is not None for ev in stages.values())
    return AuditReport(
        run_id=state.run_id,
        started_at=state.started_at,
        completed_at=datetime.now(UTC) if complete else None,
        target_host=state.target_host,
        engram_revision=state.engram_revision,
        hermes_revision=state.hermes_revision,
        identity_preflight=state.stage("stage_0_identity_preflight"),
        fixtures=dict(state.fixtures),
        stages=stages,
        negative_controls=dict(state.negative_controls),
    )


def _overall(
    stages: dict[str, StageEvidence], negative: dict[str, StageEvidence]
) -> dict[str, Any]:
    """Roll up per-stage statuses into overall status / findings.

    A single failed stage -> failed. No failures but any finding or any
    incomplete (blocked/not_run) stage -> partial. All pass/pass_expected_denial
    -> pass. ``finding`` statuses are collected into findings[], never failed.
    An empty run (no stages recorded) is ``partial`` — nothing was proven.
    """
    canonical = {name: stages.get(name, StageEvidence()) for name in STAGE_ORDER}
    failed: list[str] = []
    findings: list[str] = []
    any_finding = False
    any_incomplete = False
    for name, ev in canonical.items():
        if ev.status in _FAILURE_STATUSES:
            failed.append(name)
        elif ev.status == "finding":
            any_finding = True
            rc = ev.reason_code or "FINDING"
            findings.append(f"{name}: {rc}")
        elif ev.status in {"blocked", "not_run"}:
            any_incomplete = True
    # Negative controls are separately reported. A required control is marked
    # by the harness, while absent optional controls do not fabricate a pass.
    for name, ev in negative.items():
        if ev.evidence.get("required") and ev.status not in _PASSING_STATUSES:
            any_incomplete = True
            findings.append(f"{name}: required control not proven")
    status = "failed" if failed else ("partial" if (any_finding or any_incomplete) else "pass")
    return {"status": status, "failed_stages": failed, "findings": findings}


# ── Schema validation ────────────────────────────────────────────────────────

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def load_schema(schema_path: str | Path | None = None) -> dict[str, Any]:
    """Load the JSON Schema for the report. Bundled schema is the default."""
    if schema_path is None:
        schema_path = (
            Path(__file__).resolve().parent.parent / "schemas" / "memory-e2e-audit-v1.schema.json"
        )
    key = str(schema_path)
    if key not in _SCHEMA_CACHE:
        with open(schema_path, encoding="utf-8") as fh:
            _SCHEMA_CACHE[key] = json.load(fh)
    return _SCHEMA_CACHE[key]


def validate_report(report: dict[str, Any], schema_path: str | Path | None = None) -> None:
    """Validate a report dict against the v1 schema.

    Uses jsonschema when available (it is a dev dependency in this repo).
    Falls back to a structural assertion of required top-level fields + the
    fixed ``schema``/``schema_version`` so the test suite and CLI are usable
    even where jsonschema is absent. The bundled Compose CI path installs the
    full dev extras, so CI always uses real jsonschema.
    """
    schema = load_schema(schema_path)
    try:
        import jsonschema  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError("jsonschema is required to validate audit reports") from exc
    jsonschema.validate(instance=report, schema=schema)


def _validate_report_fallback(report: dict[str, Any]) -> None:
    if report.get("schema") != SCHEMA_NAME:
        raise ValueError(f"report.schema must be {SCHEMA_NAME!r}")
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"report.schema_version must be {SCHEMA_VERSION!r}")
    for req in (
        "run_id",
        "started_at",
        "target",
        "identity_preflight",
        "fixtures",
        "stages",
        "negative_controls",
        "overall",
    ):
        if req not in report:
            raise ValueError(f"report missing required field: {req}")
    for fk in ("write", "recall", "epistemic"):
        if fk not in report["fixtures"]:
            raise ValueError(f"report.fixtures missing: {fk}")


# ── Exception -> reason-code mapping (deterministic, no raw bodies) ───────────


def reason_code_from_exception(exc: BaseException, *, default: str = "UNKNOWN_ERROR") -> str:
    """Map an exception to a stable categorical reason code.

    Never uses ``str(exc)`` in the report — exception messages may contain
    bound values or partial secrets. The caller may store a *redacted* snippet
    in evidence separately.
    """
    name = type(exc).__name__
    cls = type(exc)
    # httpx / transport
    if cls.__module__ == "httpx" or "Connect" in name or "Timeout" in name:
        return "RECALL_REQUEST_FAILED"
    if "Auth" in name or "Unauthorized" in name:
        return "IDENTITY_AUTH_FAILED"
    if "NotFound" in name:
        return "ENGRAM_ITEM_NOT_FOUND"
    return default


# ── Helpers ──────────────────────────────────────────────────────────────────


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def _from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return _iso(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    raise TypeError(f"not JSON serializable: {type(obj).__name__}")


def _sanitize_evidence(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact secrets in an evidence dict (strings only)."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = _sanitize_value(v)
    return out


def _sanitize_value(v: Any) -> Any:
    if isinstance(v, str):
        return redact_secrets(v)
    if isinstance(v, dict):
        return _sanitize_evidence(v)
    if isinstance(v, list):
        return [_sanitize_value(x) for x in v]
    if isinstance(v, (datetime,)):
        return _iso(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


# Secret-assertion patterns for the on-disk invariant. These intentionally do
# NOT include the API-key regex (a real Engram item id like
# ``eng_abc123_...``? No — item ids are UUIDs, and run markers like
# ``AUDIT-WRITE-<uuid>`` never start with ``eng_``). We assert on the
# high-signal credential shapes: Bearer headers and DSN passwords.
_SECRET_ASSERT_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{12,}"),
    re.compile(r"postgres(?:ql)?(?:\+asyncpg)?://[^\s:/@]+:[^\s/@]{4,}@"),
    # bare-looking api keys (eng_<id>_<long secret>) — long enough to be real.
    re.compile(r"eng_[A-Za-z0-9]{10,}_[A-Za-z0-9_\-]{16,}"),
)


def assert_no_secrets(text: str, *, context: str) -> None:
    """Assert that ``text`` contains no recognizable secret shapes.

    Used as the on-disk invariant for run state and as a verification step in
    tests. A hit is a hard error — the harness must never persist credentials.
    """
    for pat in _SECRET_ASSERT_PATTERNS:
        m = pat.search(text)
        if m:
            raise AssertionError(
                f"secret-shaped string found in {context}: pattern {pat.pattern!r} matched"
            )
