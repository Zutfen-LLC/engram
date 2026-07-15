from __future__ import annotations

from uuid import uuid4

from engram.candidate_ingests import CandidateIdentity, identity_mismatches
from engram.models import CandidateIngest


def test_identity_mismatches_reports_only_safe_categories() -> None:
    ingest = CandidateIngest(
        tenant_id=uuid4(),
        principal_id=uuid4(),
        workspace_id=uuid4(),
        source_type="manual",
        content_hash="stored-hash",
    )
    supplied = CandidateIdentity(
        tenant_id=uuid4(),
        principal_id=uuid4(),
        workspace_id=uuid4(),
        source_type="import",
        content_hash="supplied-hash",
    )
    assert identity_mismatches(ingest, supplied) == (
        "tenant_mismatch",
        "principal_mismatch",
        "workspace_mismatch",
        "source_type_mismatch",
        "content_hash_mismatch",
    )
