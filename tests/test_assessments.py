"""Assessment lifecycle proofs through HTTP and the worker job boundary."""

import pytest

from engram.config import settings
from tests.test_extraction import extraction_stack  # noqa: F401


@pytest.fixture
async def assessment_stack(extraction_stack, monkeypatch):  # noqa: F811
    from sqlalchemy import text

    from engram.auth import Principal, get_current_principal

    *_, app, principal = extraction_stack
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        principal.tenant_id, principal.principal_id, ("read", "write", "review")
    )
    monkeypatch.setattr(settings, "assessment_reassessment_enabled", True)
    monkeypatch.setattr(settings, "classification_provider", "none")
    yield extraction_stack
    _, _, owner, _, _, pid, _, _ = extraction_stack
    async with owner.begin() as conn:
        await conn.execute(
            text("DELETE FROM assessment_requests WHERE principal_id=:p"), {"p": pid}
        )


async def test_explicit_kind_can_request_assessment_without_changing_promotion(
    assessment_stack,
):
    client, *_ = assessment_stack
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Keep the database migration audit records.",
            "kind": "fact",
            "source_type": "sync_turn",
        },
    )
    assert response.status_code == 201, response.text
    item_id = response.json()["id"]
    response = await client.post(f"/v1/items/{item_id}/reassess", json={"reason": "manual"})
    assert response.status_code == 200, response.text
    first = response.json()
    second = await client.post(f"/v1/items/{item_id}/reassess", json={"reason": "manual"})
    assert second.json()["request_id"] == first["request_id"]
    detail = (await client.get(f"/v1/items/{item_id}")).json()
    assert detail["item"]["kind"] == "fact"
    assert detail["item"]["review_status"] == "proposed"
    history = await client.get(f"/v1/items/{item_id}/assessments")
    assert history.status_code == 200
    assert history.json()["effective"] == {}


async def create_item(client):
    response = await client.post(
        "/v1/remember",
        json={
            "content": "Keep deployment audit records.",
            "kind": "fact",
            "source_type": "sync_turn",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def run_job(stack):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from engram.worker import process_one_job

    _, _, owner, factory, *_ = stack
    return await process_one_job(
        worker_id="assessment-test",
        session_factory=async_sessionmaker(owner, expire_on_commit=False),
        app_session_factory=factory,
        job_types=["assessment.reassess"],
    )


async def test_disabled_provider_records_unknown_and_visible_retry(assessment_stack):
    client, *_ = assessment_stack
    item_id = await create_item(client)
    response = await client.post(f"/v1/items/{item_id}/reassess", json={})
    request_id = response.json()["request_id"]
    await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert len(history["assessments"]) == 1
    assessment = history["assessments"][0]
    assert assessment["state"] == "disabled"
    assert assessment["dimensions"]["retention"]["raw_value"] is None
    assert assessment["dimensions"]["epistemic_state"] == "unknown"
    assert "provider_disabled" in assessment["dimensions"]["reason_codes"]
    status = (await client.get(f"/v1/items/{item_id}/reassessments/{request_id}")).json()
    assert status["job_status"] == "pending"
    assert status["attempts"] == 1


def provider_transport(monkeypatch, handler=None):
    import json

    import httpx
    from openai import AsyncOpenAI

    from engram import assessment_provider

    async def response(request):
        if handler:
            return await handler(request)
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "suggested_kind": "decision",
                                    "taxonomy_value": 0.9,
                                    "retention_value": 0.9,
                                    "retention_disposition": "retain",
                                }
                            ),
                        },
                    }
                ],
            },
        )

    monkeypatch.setattr(
        assessment_provider,
        "AsyncOpenAI",
        lambda **kw: AsyncOpenAI(
            **kw,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        ),
    )
    monkeypatch.setattr(settings, "classification_provider", "openai")
    monkeypatch.setattr(settings, "classification_api_key", "fixture-key")


async def test_provider_recovery_keeps_history_and_governed_kind(assessment_stack, monkeypatch):
    from engram.assessments import current_contract
    from engram.extraction import digest

    client, *_ = assessment_stack
    item_id = await create_item(client)
    await client.post(f"/v1/items/{item_id}/reassess", json={})
    await run_job(assessment_stack)
    provider_transport(monkeypatch)
    monkeypatch.setattr(settings, "assessment_selection_enabled", True)
    monkeypatch.setattr(
        settings,
        "assessment_effective_contract_hash",
        digest(current_contract().model_dump(mode="json")),
    )
    recovered = await client.post(
        f"/v1/items/{item_id}/reassess", json={"reason": "provider_recovery"}
    )
    assert recovered.status_code == 200, recovered.text
    await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert len(history["assessments"]) == 2
    effective = history["effective"]["combined"]
    assert effective["state"] == "completed"
    assert effective["dimensions"]["retention"]["raw_value"] == 0.9
    assert effective["dimensions"]["retention"]["calibrated_value"] is None
    assert effective["dimensions"]["epistemic_state"] == "unknown"
    assert effective["prior_assessment_id"] == history["assessments"][1]["assessment_id"]
    detail = (await client.get(f"/v1/items/{item_id}")).json()["item"]
    assert detail["kind"] == "fact"
    assert detail["review_status"] == "proposed"


async def test_human_review_during_provider_call_makes_result_stale(assessment_stack, monkeypatch):
    import asyncio
    import json

    import httpx

    client, *_ = assessment_stack
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        started.set()
        await finish.wait()
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"retention_value": 0.9}),
                        },
                    }
                ],
            },
        )

    item_id = await create_item(client)
    provider_transport(monkeypatch, delayed)
    await client.post(f"/v1/items/{item_id}/reassess", json={})
    work = asyncio.create_task(run_job(assessment_stack))
    try:
        await asyncio.wait_for(started.wait(), 5)
        reviewed = await client.post(
            f"/v1/items/{item_id}/review", json={"review_status": "disputed"}
        )
        assert reviewed.status_code == 200, reviewed.text
    finally:
        finish.set()
        await work
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert history["assessments"][0]["state"] == "stale"
    assert history["effective"] == {}


async def test_recorded_origin_does_not_turn_retention_into_factual_support(
    assessment_stack,
    monkeypatch,
):
    from tests.test_extraction import configure, proposition, request

    client, extraction_provider, *_ = assessment_stack
    content = "The deployment is independently verified."
    configure(extraction_provider, proposition(content, role="inference"))
    captured = await client.post(
        "/v1/extract",
        json=request(
            mode="write_proposed",
            idempotency_key="assessment-origin",
            messages=[{"message_id": "u", "role": "assistant", "content": content}],
        ),
    )
    assert captured.status_code == 200, captured.text
    item_id = captured.json()["receipt"]["candidates"][0]["memory_item_id"]
    provider_transport(monkeypatch)
    await client.post(f"/v1/items/{item_id}/reassess", json={"purpose": "epistemic"})
    await run_job(assessment_stack)
    result = (await client.get(f"/v1/items/{item_id}/assessments")).json()["assessments"][0]
    assert result["dimensions"]["epistemic_state"] == "insufficient_evidence"
    assert result["dimensions"]["assertion_mode"] == "inference"
    assert result["dimensions"]["origin"] == "assistant"
    assert result["dimensions"]["epistemic"]["calibrated_value"] is None


async def test_classify_compatibility_preserves_unknown_in_sdk(assessment_stack):
    from engram_client.models import ClassifyResponse

    client, *_ = assessment_stack
    response = await client.post("/v1/classify", json={"content": "Keep deployment records."})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["confidence"] == body["taxonomy_confidence"]
    assert body["retention_confidence"] == 0
    assert body["assessment_dimensions"]["retention"]["raw_value"] is None
    parsed = ClassifyResponse.model_validate(body).model_dump(mode="json")
    assert parsed["assessment_dimensions"]["epistemic"]["calibrated_value"] is None


async def test_explicit_kind_capture_enqueues_assessment_when_enabled(
    assessment_stack, monkeypatch
):
    client, *_ = assessment_stack
    monkeypatch.setattr(settings, "assessment_capture_enabled", True)
    item_id = await create_item(client)
    assert await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert history["assessments"][0]["state"] == "disabled"


async def test_duplicate_requests_two_workers_and_model_upgrade(assessment_stack, monkeypatch):
    import asyncio

    from engram.assessments import current_contract
    from engram.extraction import digest

    client, *_ = assessment_stack
    item_id = await create_item(client)
    provider_transport(monkeypatch)
    monkeypatch.setattr(settings, "assessment_selection_enabled", True)
    pinned = digest(current_contract().model_dump(mode="json"))
    monkeypatch.setattr(settings, "assessment_effective_contract_hash", pinned)
    requests = await asyncio.gather(
        *(client.post(f"/v1/items/{item_id}/reassess", json={}) for _ in range(2))
    )
    assert all(r.status_code == 200 for r in requests)
    assert requests[0].json()["request_id"] == requests[1].json()["request_id"]
    await asyncio.gather(run_job(assessment_stack), run_job(assessment_stack))
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert len(history["assessments"]) == 1
    original = history["effective"]["combined"]["assessment_id"]
    monkeypatch.setattr(settings, "classification_model", "upgraded-model")
    await client.post(f"/v1/items/{item_id}/reassess", json={"reason": "model_upgrade"})
    await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert len(history["assessments"]) == 2
    assert history["effective"]["combined"]["assessment_id"] == original
    assert history["effective"]["combined"]["contract_hash"] == pinned


async def test_dead_provider_job_can_recover_without_rewriting_failure(
    assessment_stack, monkeypatch
):
    import httpx

    client, *_ = assessment_stack
    item_id = await create_item(client)

    async def failed(request):
        return httpx.Response(503, json={"error": {"message": "fixture unavailable"}})

    provider_transport(monkeypatch, failed)
    monkeypatch.setattr(settings, "job_max_attempts", 1)
    response = await client.post(f"/v1/items/{item_id}/reassess", json={})
    request_id = response.json()["request_id"]
    await run_job(assessment_stack)
    status_url = f"/v1/items/{item_id}/reassessments/{request_id}"
    assert (await client.get(status_url)).json()["job_status"] == "dead"
    first = (await client.get(f"/v1/items/{item_id}/assessments")).json()["assessments"][0]
    provider_transport(monkeypatch)
    retry = await client.post(status_url + "/retry", json={"reason": "provider_recovery"})
    assert retry.status_code == 200, retry.text
    await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert history["assessments"][1] == first
    assert history["assessments"][0]["state"] == "completed"
    assert (await client.get(status_url)).json()["job_status"] == "succeeded"


async def test_assessment_access_follows_item_and_review_authority(assessment_stack):
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from engram.auth import Principal, get_current_principal
    from engram.db import apply_rls_context

    client, _, _, factory, tid, pid, app, principal = assessment_stack
    item_id = await create_item(client)
    response = await client.post(f"/v1/items/{item_id}/reassess", json={})
    request_id = response.json()["request_id"]
    await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assessment_id = history["assessments"][0]["assessment_id"]
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        principal.tenant_id,
        principal.principal_id,
        ("read",),
    )
    assert (await client.get(f"/v1/items/{item_id}/assessments")).status_code == 200
    assert (
        await client.get(f"/v1/items/{item_id}/assessments/{assessment_id}/debug")
    ).status_code == 403
    assert (await client.post(f"/v1/items/{item_id}/reassess", json={})).status_code == 403
    async with factory() as db:
        await apply_rls_context(db, tenant_id=uuid4(), principal_id=pid)
        for table in ("assessment_requests", "memory_assessments"):
            assert await db.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        await db.rollback()
        await apply_rls_context(db, tenant_id=tid, principal_id=uuid4())
        for table in ("assessment_requests", "memory_assessments"):
            assert (
                await db.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE memory_item_id=:i"), {"i": item_id}
                )
                == 0
            )
        await db.rollback()
        await apply_rls_context(db, tenant_id=tid, principal_id=pid)
        for command in (
            "UPDATE memory_assessments SET state='completed' WHERE id=:i",
            "DELETE FROM memory_assessments WHERE id=:i",
        ):
            with pytest.raises(DBAPIError):
                async with db.begin_nested():
                    await db.execute(text(command), {"i": assessment_id})
    assert request_id is not None


async def test_assessment_queue_rotates_between_tenants(assessment_stack):
    from uuid import uuid4

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from engram.jobs import claim_next_job, enqueue_job_in_transaction, mark_job_succeeded

    _, _, owner, _, tid, *_ = assessment_stack
    other = uuid4()
    factory = async_sessionmaker(owner, expire_on_commit=False)
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO tenants(id,name,slug) VALUES (:t,:s,:s)"),
            {"t": other, "s": f"assessment-{other}"},
        )
    job_ids = []
    try:
        async with factory() as db:
            for tenant in (tid, tid, other):
                job_ids.append(
                    await enqueue_job_in_transaction(
                        db,
                        tenant_id=tenant,
                        job_type="assessment.reassess",
                        payload={},
                    )
                )
            await db.commit()
            first = await claim_next_job(
                db, worker_id="fairness", job_types=["assessment.reassess"]
            )
            assert first.tenant_id == tid
            await mark_job_succeeded(db, first.id)
            second = await claim_next_job(
                db, worker_id="fairness", job_types=["assessment.reassess"]
            )
            assert second.tenant_id == other
    finally:
        async with owner.begin() as conn:
            for job_id in job_ids:
                await conn.execute(text("DELETE FROM jobs WHERE id=:i"), {"i": job_id})
            await conn.execute(text("DELETE FROM tenants WHERE id=:i"), {"i": other})


async def test_migration_backfills_legacy_receipt_without_epistemic_evidence(assessment_stack):
    import os
    from pathlib import Path

    import asyncpg

    from engram.migrations import normalize_asyncpg_url

    client, *_ = assessment_stack
    content = "Keep an audit trail for schema changes."
    classified = (
        await client.post(
            "/v1/classify",
            json={
                "content": content,
                "source_type": "sync_turn",
            },
        )
    ).json()
    captured = await client.post(
        "/v1/remember",
        json={
            "content": content,
            "kind": classified["suggested_kind"],
            "source_type": "sync_turn",
            "classification_run_id": classified["classification_run_id"],
        },
    )
    assert captured.status_code == 201, captured.text
    item_id = captured.json()["id"]
    conn = await asyncpg.connect(normalize_asyncpg_url(os.environ["ENGRAM_OWNER_DATABASE_URL"]))
    try:
        await conn.execute(Path("migrations/037_memory_assessments.sql").read_text())
        first = (await client.get(f"/v1/items/{item_id}/assessments")).json()
        row = first["assessments"][0]
        assert row["state"] == "legacy"
        assert row["dimensions"]["taxonomy"]["raw_value"] == pytest.approx(
            classified["taxonomy_confidence"],
        )
        assert row["dimensions"]["retention"]["raw_value"] == classified["retention_confidence"]
        assert row["dimensions"]["epistemic_state"] == "unknown"
        await conn.execute(Path("migrations/037_memory_assessments.sql").read_text())
        second = (await client.get(f"/v1/items/{item_id}/assessments")).json()
        assert second == first
    finally:
        await conn.close()


async def test_profile_cannot_read_request_or_select_effective_assessment(assessment_stack):
    from dataclasses import replace
    from uuid import uuid4

    from engram.auth import Principal
    from engram.memory_context import resolve_memory_context, unrestricted_memory_context

    client, _, _, _, tid, pid, app, _ = assessment_stack
    item_id = await create_item(client)
    row = (await client.post(f"/v1/items/{item_id}/reassess", json={})).json()
    denied = replace(
        unrestricted_memory_context(Principal(str(tid), str(pid), ("admin",))),
        memory_profile_id=uuid4(),
        readable_workspace_ids=frozenset(),
        writable_workspace_ids=frozenset(),
        include_private=False,
        include_tenant=False,
        include_public=False,
    )
    app.dependency_overrides[resolve_memory_context] = lambda: denied
    for path in (
        f"/v1/items/{item_id}",
        f"/v1/items/{item_id}/assessments",
        f"/v1/items/{item_id}/reassessments/{row['request_id']}",
    ):
        assert (await client.get(path)).status_code == 404
    assert (await client.post(f"/v1/items/{item_id}/reassess", json={})).status_code == 404


@pytest.mark.parametrize(
    "taxonomy,retention,expected", [(0.2, 0.8, 0.8), (0.9, None, None), (0.9, 0, 0)]
)
async def test_retention_availability_is_independent_of_taxonomy(
    assessment_stack,
    monkeypatch,
    taxonomy,
    retention,
    expected,
):
    import json

    import httpx

    from engram import assessment_provider, classification

    async def output(request):
        payload = {
            "suggested_kind": "fact",
            "taxonomy_confidence": taxonomy,
            "retention_disposition": "retain",
        }
        if retention is not None:
            payload["retention_confidence"] = retention
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(payload),
                        },
                    }
                ],
            },
        )

    client, *_ = assessment_stack
    provider_transport(monkeypatch, output)
    monkeypatch.setattr(classification, "AsyncOpenAI", assessment_provider.AsyncOpenAI)
    response = await client.post("/v1/classify", json={"content": "Keep deployment audit records."})
    assert response.status_code == 200, response.text
    assert response.json()["assessment_dimensions"]["retention"]["raw_value"] == expected


async def test_calibration_uses_verified_snapshot_during_provider_call(
    assessment_stack,
    monkeypatch,
    tmp_path,
):
    import json

    import httpx

    from engram.assessment_calibration import CalibrationProfile
    from engram.assessments import current_contract

    client, *_ = assessment_stack
    item_id = await create_item(client)
    path = tmp_path / "profiles.json"
    profiles = []

    async def replace_artifact(request):
        replacement = json.loads(path.read_text())
        replacement[0]["bins"][0]["value"] = 0.2
        path.write_text(json.dumps(replacement))
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"retention_value": 0.9}),
                        },
                    }
                ],
            },
        )

    provider_transport(monkeypatch, replace_artifact)
    monkeypatch.setattr(settings, "classification_model", "fixture")
    profile = CalibrationProfile(
        version="fixture-v1",
        contract=current_contract(),
        dataset_version="labeled-fixture",
        dimension="retention",
        source_type="sync_turn",
        assertion_mode="unknown",
        kind="fact",
        risk="unknown",
        bins=[{"lower": 0, "upper": 1, "value": 0.7, "count": 100}],
    )
    profiles.append(profile.model_dump(mode="json"))
    path.write_text(json.dumps(profiles))
    monkeypatch.setattr(settings, "assessment_calibration_profiles_path", str(path))
    monkeypatch.setattr(settings, "assessment_calibration_version", "fixture-v1")
    response = await client.post(f"/v1/items/{item_id}/reassess", json={})
    assert response.status_code == 200, response.text
    await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    result = history["assessments"][0]["dimensions"]["retention"]
    assert result["calibrated_value"] == 0.7


@pytest.mark.parametrize("change", ["content", "provenance"])
async def test_changed_content_or_evidence_cannot_bind(assessment_stack, monkeypatch, change):
    import json

    import httpx
    from sqlalchemy import text

    from tests.test_extraction import configure, proposition
    from tests.test_extraction import request as extraction_request

    client, extractor, owner, *_ = assessment_stack
    item_id = await create_item(client)

    async def change_input(request):
        if change == "content":
            # Simulate an incompatible writer outside the append-first API.
            async with owner.begin() as conn:
                await conn.execute(
                    text("UPDATE memory_items SET content=:c WHERE id=:i"),
                    {"c": "Changed by an incompatible writer.", "i": item_id},
                )
        else:
            content = "Keep deployment audit records."
            configure(extractor, proposition(content))
            extracted = await client.post(
                "/v1/extract",
                json=extraction_request(
                    mode="write_proposed",
                    idempotency_key="new-evidence",
                    messages=[{"message_id": "u", "role": "user", "content": content}],
                ),
            )
            assert extracted.status_code == 200, extracted.text
            assert extracted.json()["receipt"]["candidates"][0]["memory_item_id"] == item_id
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"retention_value": 0.9}),
                        },
                    }
                ],
            },
        )

    provider_transport(monkeypatch, change_input)
    await client.post(f"/v1/items/{item_id}/reassess", json={})
    await run_job(assessment_stack)
    history = (await client.get(f"/v1/items/{item_id}/assessments")).json()
    assert history["assessments"][0]["state"] == "stale"
    assert history["effective"] == {}
