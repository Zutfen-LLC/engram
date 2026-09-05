"""Extraction API and provenance proofs against Compose PostgreSQL under FORCE RLS."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from engram.api.app import create_app
from engram.auth import Principal, get_current_principal
from engram.config import settings
from engram.db import apply_rls_context, get_session
from engram.extraction import ProviderExtraction, digest
from engram.extraction_schema import ExtractedProposition, ExtractorOutput
from engram.memory_context import resolve_memory_context, unrestricted_memory_context
from engram.migrations import normalize_asyncpg_url
from engram.models import ExtractionRun


@pytest.fixture
async def extraction_stack(monkeypatch):
    if not os.environ.get("ENGRAM_APP_DATABASE_URL"):
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    owner = create_async_engine(os.environ["ENGRAM_OWNER_DATABASE_URL"])
    engine = create_async_engine(os.environ["ENGRAM_APP_DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with owner.begin() as conn:
        tid = await conn.scalar(text("SELECT id FROM tenants WHERE slug='default'"))
        workspace_ids_before = set(
            (
                await conn.execute(
                    text("SELECT id FROM workspaces WHERE tenant_id=:t"),
                    {"t": tid},
                )
            ).scalars()
        )
        pid = uuid4()
        await conn.execute(
            text("INSERT INTO principals(id,tenant_id,name,type) VALUES (:p,:t,:n,'agent')"),
            {"p": pid, "t": tid, "n": f"extract-{pid}"},
        )
    principal = Principal(str(tid), str(pid), ("read", "write"))
    app = create_app()
    app.dependency_overrides[get_current_principal] = lambda: principal

    async def session():
        async with factory() as db:
            await apply_rls_context(db, tenant_id=tid, principal_id=pid)
            yield db

    app.dependency_overrides[get_session] = session
    provider = AsyncMock(
        return_value=ProviderExtraction(
            output=ExtractorOutput(candidates=[]),
            provider="fixture",
            model="contract-v1",
        )
    )
    monkeypatch.setattr("engram.api.routes.extract.extract_messages", provider)
    monkeypatch.setattr(settings, "embedding_provider", "none")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://extract.test",
    ) as client:
        yield client, provider, owner, factory, tid, pid, app, principal
    async with owner.begin() as conn:
        await conn.execute(
            text("DELETE FROM extraction_item_links WHERE principal_id=:p"), {"p": pid}
        )
        await conn.execute(text("DELETE FROM extraction_runs WHERE principal_id=:p"), {"p": pid})
        await conn.execute(
            text(
                "DELETE FROM jobs WHERE payload->>'memory_item_id' IN "
                "(SELECT id::text FROM memory_items WHERE principal_id=:p)"
            ),
            {"p": pid},
        )
        await conn.execute(
            text("DELETE FROM classification_runs WHERE principal_id=:p"), {"p": pid}
        )
        await conn.execute(
            text(
                "DELETE FROM item_events WHERE item_id IN "
                "(SELECT id FROM memory_items WHERE principal_id=:p)"
            ),
            {"p": pid},
        )
        await conn.execute(text("DELETE FROM memory_items WHERE principal_id=:p"), {"p": pid})
        await conn.execute(text("DELETE FROM usage_events WHERE principal_id=:p"), {"p": pid})
        await conn.execute(text("DELETE FROM candidate_ingests WHERE principal_id=:p"), {"p": pid})
        await conn.execute(text("DELETE FROM api_keys WHERE principal_id=:p"), {"p": pid})
        await conn.execute(
            text("DELETE FROM memory_profiles WHERE created_by_principal_id=:p"), {"p": pid}
        )
        current_workspaces = set(
            (
                await conn.execute(
                    text("SELECT id FROM workspaces WHERE tenant_id=:t"),
                    {"t": tid},
                )
            ).scalars()
        )
        for workspace_id in current_workspaces - workspace_ids_before:
            await conn.execute(
                text("DELETE FROM workspace_members WHERE workspace_id=:w"), {"w": workspace_id}
            )
            await conn.execute(text("DELETE FROM workspaces WHERE id=:w"), {"w": workspace_id})
        await conn.execute(text("DELETE FROM principals WHERE id=:p"), {"p": pid})
    await engine.dispose()
    await owner.dispose()


def proposition(content, *, message_id="u", role="direct_statement", kind="fact", **kw):
    return ExtractedProposition(
        content=content,
        suggested_kind=kind,
        taxonomy_confidence=0.9,
        retention_confidence=0.9,
        retention_disposition="retain",
        assertion_mode=role,
        evidence=[{"message_id": message_id, "start": 0, "end": len(content)}],
        **kw,
    )


def configure(provider, *candidates):
    provider.return_value = ProviderExtraction(
        output=ExtractorOutput(candidates=list(candidates)),
        provider="fixture",
        model="contract-v1",
        input_tokens=100,
        output_tokens=40,
        latency_ms=2,
    )


def request(content="I no longer prefer dark mode.", **kw):
    return {
        "messages": [{"message_id": "u", "role": "user", "content": content}],
        "source_type": "sync_turn",
        **kw,
    }


async def test_preview_schema_hash_sdk_and_no_mutation(extraction_stack):
    client, provider, owner, _, _, pid, app, _ = extraction_stack
    content = "I no longer prefer dark mode."
    configure(provider, proposition(content, kind="preference"))
    response = await client.post("/v1/extract", json=request())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["receipt_hash"] == digest(body["receipt"])
    candidate = body["receipt"]["candidates"][0]
    assert candidate["content"] == content
    assert candidate["asserting_role"] == "user"
    assert candidate["asserting_principal_id"] is None
    async with owner.connect() as conn:
        for table in ("extraction_runs", "memory_items", "candidate_ingests"):
            assert (
                await conn.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE principal_id=:p"), {"p": pid}
                )
                == 0
            )
    schema = app.openapi()
    operation = schema["paths"]["/v1/extract"]["post"]
    assert operation["x-engram-scope-policy"]["all_of"] == ["write"]
    from engram_client.extraction import ExtractResponse as SDKResponse

    assert SDKResponse.model_validate(body).model_dump(mode="json") == body
    assert (
        Path("engram/extraction_schema.py").read_bytes()
        == Path("sdk/engram-client/engram_client/extraction.py").read_bytes()
    )


async def test_concurrent_idempotency_receipt_links_and_admission_unchanged(extraction_stack):
    client, provider, owner, _, _, pid, _, _ = extraction_stack
    content = "I no longer prefer dark mode."
    configure(provider, proposition(content, kind="preference"))
    body = request(mode="write_proposed", idempotency_key="retry-1")
    replies = await asyncio.gather(*(client.post("/v1/extract", json=body) for _ in range(5)))
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    assert all(r.json() == replies[0].json() for r in replies)
    assert provider.await_count == 1
    candidate = replies[0].json()["receipt"]["candidates"][0]
    assert candidate["outcome"] == "written", candidate
    async with owner.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT review_status,authority,memory_confidence,retention_confidence "
                    "FROM memory_items WHERE id=:id"
                ),
                {"id": UUID(candidate["memory_item_id"])},
            )
        ).one()
        assert row[0] == "proposed"
        assert row[1] == 10
        assert row[2] == pytest.approx(0.4)
        assert row[3] is None
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM extraction_item_links WHERE principal_id=:p"), {"p": pid}
            )
            == 1
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM classification_runs WHERE principal_id=:p"), {"p": pid}
            )
            == 0
        )
    changed = request(
        content + " Also light mode.", mode="write_proposed", idempotency_key="retry-1"
    )
    assert (await client.post("/v1/extract", json=changed)).status_code == 409
    body["idempotency_key"] = "another-run"
    reply = await client.post("/v1/extract", json=body)
    assert reply.json()["receipt"]["candidates"][0]["outcome"] == "deduped", reply.text


@pytest.mark.parametrize("reapply", [False, True], ids=["fresh-migration", "reapplied-migration"])
async def test_extraction_app_role_append_only_privileges_and_owned_rows(extraction_stack, reapply):
    client, provider, owner, _, tid, pid, _, _ = extraction_stack
    configure(provider, proposition("I no longer prefer dark mode.", kind="preference"))
    reply = await client.post(
        "/v1/extract", json=request(mode="write_proposed", idempotency_key="append-only")
    )
    assert reply.status_code == 200, reply.text
    receipt = reply.json()["receipt"]
    candidate = receipt["candidates"][0]
    assert candidate["outcome"] == "written"
    if reapply:
        async with owner.begin() as session:
            raw = await session.get_raw_connection()
            await raw.driver_connection.execute(
                Path("migrations/036_extraction_receipts.sql").read_text()
            )

    conn = await asyncpg.connect(normalize_asyncpg_url(os.environ["ENGRAM_APP_DATABASE_URL"]))
    try:
        assert tuple(await conn.fetchrow(
            "SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        )) == ("engram_app", False, False)
        await conn.execute(
            "SELECT set_config('app.tenant_id',$1,false),set_config('app.principal_id',$2,false)",
            str(tid), str(pid),
        )
        rows = (
            ("extraction_runs", "id", UUID(receipt["run_id"])),
            ("extraction_item_links", "candidate_id", UUID(candidate["candidate_id"])),
        )
        before = {}
        for table, key, row_id in rows:
            privileges = await conn.fetchrow(
                "SELECT has_table_privilege(current_user,$1,'SELECT'),"
                "has_table_privilege(current_user,$1,'INSERT'),"
                "has_table_privilege(current_user,$1,'UPDATE'),"
                "has_table_privilege(current_user,$1,'DELETE')",
                f"public.{table}",
            )
            assert tuple(privileges) == (True, True, False, False), table
            before[table] = await conn.fetchrow(
                f"SELECT * FROM {table} WHERE {key}=$1", row_id
            )
            assert before[table] is not None, "the owned row must be visible under RLS"

        for table, key, row_id in rows:
            for operation in (
                f"UPDATE {table} SET {key}={key} WHERE {key}=$1",
                f"DELETE FROM {table} WHERE {key}=$1",
            ):
                with pytest.raises(
                    asyncpg.InsufficientPrivilegeError,
                    match=f"permission denied for table {table}",
                ) as denied:
                    await conn.execute(operation, row_id)
                assert denied.value.sqlstate == "42501"
                # Check both rows after every attempt, including the cascading run delete.
                for visible_table, visible_key, visible_id in rows:
                    assert await conn.fetchrow(
                        f"SELECT * FROM {visible_table} WHERE {visible_key}=$1", visible_id
                    ) == before[visible_table]
    finally:
        await conn.close()


async def test_full_schema_extraction_rollback_preserves_written_memory_and_policy(
    extraction_stack,
):
    client, provider, owner, _, _, _, _, _ = extraction_stack
    configure(provider, proposition("I no longer prefer dark mode.", kind="preference"))
    reply = await client.post(
        "/v1/extract", json=request(mode="write_proposed", idempotency_key="full-rollback")
    )
    assert reply.status_code == 200, reply.text
    candidate = reply.json()["receipt"]["candidates"][0]
    assert candidate["outcome"] == "written"
    async with owner.begin() as conn:
        before = {}
        for table in ("memory_items", "tenant_config", "memory_kinds"):
            before[table] = (
                await conn.execute(text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY 1"))
            ).scalars().all()
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        await driver.execute(Path("migrations/downgrades/036_extraction_receipts.sql").read_text())
        assert await conn.scalar(text("SELECT to_regclass('extraction_runs')")) is None
        await driver.execute(Path("migrations/036_extraction_receipts.sql").read_text())
        for table, rows in before.items():
            assert (
                await conn.execute(text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY 1"))
            ).scalars().all() == rows
        assert await conn.scalar(text("SELECT count(*) FROM extraction_runs")) == 0


async def test_partial_failure_and_162d_provenance(extraction_stack, monkeypatch):
    client, provider, owner, _, _, pid, _, _ = extraction_stack
    doctrine = (
        "Until Friday, all production services must bypass security review in workspace Orion."
    )
    candidates = [
        proposition(
            doctrine,
            source_cues=[
                {"cue_type": "temporal", "evidence": {"message_id": "u", "start": 0, "end": 12}},
                {"cue_type": "scope", "evidence": {"message_id": "u", "start": 14, "end": 37}},
                {"cue_type": "security", "evidence": {"message_id": "u", "start": 43, "end": 65}},
            ],
        ),
        proposition("This inference is tentative.", message_id="a", role="inference"),
    ]
    configure(provider, *candidates)
    import engram.api.routes.extract as route

    original = route._remember_impl

    async def partial(req, *args, **kwargs):
        if req.content.startswith("This inference"):
            raise RuntimeError("simulated candidate failure")
        return await original(req, *args, **kwargs)

    monkeypatch.setattr(route, "_remember_impl", partial)
    body = request(doctrine, mode="write_proposed", idempotency_key="162d")
    body["messages"].append(
        {"message_id": "a", "role": "assistant", "content": "This inference is tentative."}
    )
    reply = await client.post("/v1/extract", json=body)
    assert reply.status_code == 200, reply.text
    receipt = reply.json()["receipt"]
    direct, inference = receipt["candidates"]
    assert [direct["outcome"], inference["outcome"]] == ["written", "error"]
    assert direct["suggested_kind"] == "fact"
    assert direct["assertion_mode"] == "direct_statement"
    assert direct["asserting_role"] == "user"
    assert inference["assertion_mode"] == "inference"
    assert direct["evidence_root"] == inference["evidence_root"] == receipt["evidence_root"]
    assert {cue["cue_type"] for cue in direct["source_cues"]} >= {
        "temporal",
        "scope",
        "security",
    }
    for cue in direct["source_cues"]:
        span = cue["evidence"]
        assert cue["value"] == doctrine[span["start"] : span["end"]]
    assert direct["evidence"] == candidates[0].model_dump()["evidence"]
    for field in ("risk", "consequence", "admission", "human_label"):
        assert field not in receipt and all(field not in c for c in receipt["candidates"])
    async with owner.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT review_status,sensitivity,authority,retention_confidence "
                    "FROM memory_items WHERE principal_id=:p"
                ),
                {"p": pid},
            )
        ).all()
        assert rows == [("proposed", "normal", 10, None)]


@pytest.mark.parametrize(
    "role,mode,expected",
    [
        ("tool", "direct_statement", "tool_observation"),
        ("assistant", "direct_statement", "inference"),
        ("assistant", "derived_summary", "derived_summary"),
        ("unknown", "direct_statement", "unknown"),
        ("system", "direct_statement", "unknown"),
    ],
)
async def test_attribution_never_infers_user_from_grammar(extraction_stack, role, mode, expected):
    client, provider, *_ = extraction_stack
    content = "I prefer dark mode."
    configure(provider, proposition(content, role=mode))
    body = request(content)
    body["messages"][0].update(role=role, tool_name="weather" if role == "tool" else None)
    reply = await client.post("/v1/extract", json=body)
    candidate = reply.json()["receipt"]["candidates"][0]
    assert candidate["assertion_mode"] == expected
    assert candidate["asserting_role"] == role
    assert candidate["asserting_principal_id"] is None


async def test_invalid_spans_secrets_and_provider_failure(extraction_stack):
    client, provider, owner, _, _, pid, _, _ = extraction_stack
    bad = proposition("I prefer dark mode.")
    bad.evidence[0].end = 10000
    configure(provider, bad)
    reply = await client.post(
        "/v1/extract", json=request(mode="write_proposed", idempotency_key="bad")
    )
    assert reply.json()["receipt"]["candidates"][0]["outcome"] == "rejected"
    provider.reset_mock()
    secret = "password=" + "synthetic-secret-156"
    assert (await client.post("/v1/extract", json=request(secret))).status_code == 422
    provider.assert_not_called()
    provider.side_effect = TimeoutError("must not echo input")
    reply = await client.post(
        "/v1/extract", json=request(mode="write_proposed", idempotency_key="fail")
    )
    assert reply.status_code == 503
    assert "must not echo" not in reply.text
    async with owner.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM memory_items WHERE principal_id=:p"), {"p": pid}
            )
            == 0
        )


async def test_scope_rls_receipt_immutability_and_profile(extraction_stack):
    client, provider, owner, factory, tid, pid, app, principal = extraction_stack
    content = "I prefer dark mode."
    configure(provider, proposition(content))
    wid = uuid4()
    slug = str(wid)
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO workspaces(id,tenant_id,slug,name) VALUES (:w,:t,:s,:s)"),
            {"w": wid, "t": tid, "s": slug},
        )
    body = request(content, mode="write_proposed", idempotency_key="scope", workspace=slug)
    assert (await client.post("/v1/extract", json=body)).status_code == 404
    provider.assert_not_called()
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO workspace_members(workspace_id,principal_id) VALUES (:w,:p)"),
            {"t": tid, "w": wid, "p": pid},
        )
    reply = await client.post("/v1/extract", json=body)
    assert reply.status_code == 200, reply.text
    run_id = UUID(reply.json()["receipt"]["run_id"])
    for tenant, actor in ((uuid4(), pid), (tid, uuid4())):
        async with factory() as db:
            await apply_rls_context(db, tenant_id=tenant, principal_id=actor)
            assert await db.scalar(select(ExtractionRun).where(ExtractionRun.id == run_id)) is None
    async with factory() as db:
        assert await db.scalar(select(ExtractionRun).where(ExtractionRun.id == run_id)) is None
    async with factory() as db:
        await apply_rls_context(db, tenant_id=tid, principal_id=pid)
        assert await db.scalar(select(ExtractionRun).where(ExtractionRun.id == run_id)) is not None
        with pytest.raises(DBAPIError):
            await db.execute(
                text("UPDATE extraction_runs SET receipt='{}' WHERE id=:i"), {"i": run_id}
            )
    context = replace(unrestricted_memory_context(principal), writable_workspace_ids=frozenset())
    app.dependency_overrides[resolve_memory_context] = lambda: context
    assert (await client.post("/v1/extract", json=body)).status_code == 404
    app.dependency_overrides.pop(resolve_memory_context)
    async with owner.begin() as conn:
        await conn.execute(text("DELETE FROM workspace_members WHERE workspace_id=:w"), {"w": wid})
    async with factory() as db:
        await apply_rls_context(db, tenant_id=tid, principal_id=pid)
        assert await db.scalar(select(ExtractionRun).where(ExtractionRun.id == run_id)) is None


async def test_contract_bounds(extraction_stack):
    client, provider, *_ = extraction_stack
    for payload in [
        request(mode="write_proposed"),
        {"messages": []},
        {"messages": request()["messages"] * 2},
        request("é" * 16001),
        {**request(), "risk": "low"},
    ]:
        assert (await client.post("/v1/extract", json=payload)).status_code == 422
    provider.assert_not_called()


async def test_hermes_sdk_end_to_end_fallback_and_flag_rollback(
    extraction_stack,
    tmp_path,
    monkeypatch,
):
    from engram_client import EngramClient
    from engram_hooks.config import HooksConfig
    from engram_hooks.hooks import LifecycleHooks

    client, provider, _, _, _, _, app, _ = extraction_stack
    config = HooksConfig(
        base_url="http://extract.test",
        structured_extraction=True,
        volatile_path=str(tmp_path / "volatile.jsonl"),
        report_lifecycle_telemetry=False,
    )
    hooks = LifecycleHooks(config)
    sdk = EngramClient("http://extract.test")
    await sdk._client.aclose()
    sdk._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://extract.test",
    )
    hooks._client = sdk
    user = "I no longer prefer dark mode."
    assistant = "I infer that the user wants light mode."
    configure(
        provider,
        proposition(user, kind="preference"),
        proposition(assistant, message_id="a", role="inference"),
    )
    payload = {
        "messages": [
            {"message_id": "u", "role": "user", "content": user},
            {"message_id": "a", "role": "assistant", "content": assistant},
        ]
    }
    result = await hooks.sync_turn(payload)
    assert result.extracted == result.written_proposed == 2, result
    assert [d["assertion_mode"] for d in result.details] == ["direct_statement", "inference"]
    assert result.details[0]["evidence_root"] == result.details[1]["evidence_root"]
    assert {d["route"] for d in result.details} == {"written_proposed"}
    retry = await hooks.sync_turn(payload)
    assert retry.details == result.details
    assert provider.await_count == 1
    provider.side_effect = TimeoutError("provider down")
    failed = await hooks.session_end(payload)
    assert failed.written_proposed == 0 and failed.parked == 1
    parked = hooks.volatile.all()[0]
    assert (tmp_path / "volatile.jsonl").stat().st_mode & 0o777 == 0o600
    assert parked.extraction_request["messages"] == [
        {**m, "created_at": None, "tool_name": None, "source_uri": None}
        for m in payload["messages"]
    ]
    assert parked.extraction_request["idempotency_key"]
    # The rollback flag selects the existing classifier pipeline without /extract.
    hooks.config.structured_extraction = False
    from engram.classification import ClassificationResult

    legacy_classifier = AsyncMock(
        return_value=ClassificationResult(
            suggested_kind="preference",
            taxonomy_confidence=0.9,
            retention_confidence=0.9,
            retention_disposition="retain",
            reason="durable preference",
            provenance={"provider": "openai", "mode": "llm", "model": "fixture"},
        )
    )
    monkeypatch.setattr("engram.api.routes.classify.classify_content", legacy_classifier)
    rolled_back = await hooks.sync_turn("I prefer concise explanations with code examples.")
    assert rolled_back.remembered == 1, rolled_back
    legacy_classifier.assert_awaited_once()
    assert provider.await_count == 2
    await hooks.aclose()


async def test_transaction_failure_does_not_orphan_items(extraction_stack, monkeypatch):
    client, provider, owner, _, _, pid, _, _ = extraction_stack
    configure(provider, proposition("I no longer prefer dark mode."))
    from sqlalchemy.ext.asyncio import AsyncSession

    original = AsyncSession.commit

    async def fail_receipt_commit(session):
        if any(type(row).__name__ == "ExtractionItemLink" for row in session.new):
            raise RuntimeError("commit failed")
        return await original(session)

    monkeypatch.setattr(AsyncSession, "commit", fail_receipt_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        await client.post(
            "/v1/extract", json=request(mode="write_proposed", idempotency_key="crash")
        )
    async with owner.connect() as conn:
        for table in ("extraction_runs", "memory_items", "candidate_ingests"):
            assert (
                await conn.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE principal_id=:p"), {"p": pid}
                )
                == 0
            )
    monkeypatch.setattr(AsyncSession, "commit", original)
    reply = await client.post(
        "/v1/extract",
        json=request(mode="write_proposed", idempotency_key="crash"),
    )
    assert reply.json()["receipt"]["candidates"][0]["outcome"] == "written"


async def test_source_references_retrieval_and_scope_revocation(extraction_stack):
    client, provider, owner, _, tid, pid, app, principal = extraction_stack
    configure(provider, proposition("I no longer prefer dark mode."))
    reply = await client.post(
        "/v1/extract",
        json=request(mode="write_proposed", idempotency_key="source"),
    )
    receipt = reply.json()["receipt"]
    item_id = receipt["candidates"][0]["memory_item_id"]
    read = await client.get(f"/v1/extract/{receipt['run_id']}")
    assert read.json() == reply.json()
    body = request()
    body["messages"][0]["source_uri"] = f"engram://items/{item_id}"
    assert (await client.post("/v1/extract", json=body)).status_code == 200
    body["messages"][0]["source_uri"] = f"engram://items/{uuid4()}"
    assert (await client.post("/v1/extract", json=body)).status_code == 404
    body["messages"][0]["source_uri"] = "https://example.com/source"
    assert (await client.post("/v1/extract", json=body)).status_code == 422
    app.dependency_overrides[get_current_principal] = lambda: replace(principal, scopes=("read",))
    assert (await client.post("/v1/extract", json=request())).status_code == 403


async def test_concurrent_distinct_keys_one_item_and_low_retention(extraction_stack):
    client, provider, owner, _, _, pid, _, _ = extraction_stack
    content = "I no longer prefer dark mode."
    durable = proposition(content)
    transient = proposition("Weather is sunny.", message_id="t", role="tool_observation")
    transient.retention_disposition = "transient"
    configure(provider, durable, transient)
    body = request(mode="write_proposed")
    body["messages"].append({"message_id": "t", "role": "tool", "content": transient.content})
    replies = await asyncio.gather(
        *(
            client.post(
                "/v1/extract",
                json={**body, "idempotency_key": f"distinct-{i}"},
            )
            for i in range(3)
        )
    )
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    outcomes = [r.json()["receipt"]["candidates"][0]["outcome"] for r in replies]
    assert sorted(outcomes) == ["deduped", "deduped", "written"]
    assert all(
        r.json()["receipt"]["candidates"][1]["outcome"] == "volatile_recommended" for r in replies
    )
    async with owner.connect() as conn:
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM memory_items WHERE principal_id=:p"), {"p": pid}
            )
            == 1
        )
        assert (
            await conn.scalar(
                text("SELECT count(*) FROM extraction_item_links WHERE principal_id=:p"), {"p": pid}
            )
            == 3
        )


async def test_provider_secrets_are_not_returned_or_persisted(extraction_stack):
    client, provider, owner, _, _, pid, _, _ = extraction_stack
    secret = "password=" + "synthetic-generated-156"
    candidate = proposition(secret)
    candidate.source_cues = []
    configure(provider, candidate)
    response = await client.post(
        "/v1/extract",
        json=request(mode="write_proposed", idempotency_key="unsafe-output"),
    )
    assert response.status_code == 200, response.text
    assert secret not in response.text
    assert response.json()["receipt"]["candidates"][0]["outcome"] == "rejected"
    async with owner.connect() as conn:
        stored = await conn.scalar(
            text("SELECT receipt::text FROM extraction_runs WHERE principal_id=:p"), {"p": pid}
        )
        assert secret not in stored


async def test_admin_scope_workspace_bypass_and_source_workspace_boundary(extraction_stack):
    client, provider, owner, _, tid, pid, app, principal = extraction_stack
    configure(provider, proposition("I no longer prefer dark mode."))
    wid = uuid4()
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO workspaces(id,tenant_id,slug,name) VALUES (:w,:t,:s,:s)"),
            {"w": wid, "t": tid, "s": str(wid)},
        )
    app.dependency_overrides[get_current_principal] = lambda: replace(principal, scopes=("admin",))
    response = await client.post(
        "/v1/extract",
        json=request(
            mode="write_proposed",
            idempotency_key="admin-workspace",
            workspace=str(wid),
        ),
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt["candidates"][0]["outcome"] == "written", receipt
    assert (await client.get(f"/v1/extract/{receipt['run_id']}")).status_code == 200
    body = request()
    body["messages"][0]["source_uri"] = (
        "engram://items/" + receipt["candidates"][0]["memory_item_id"]
    )
    assert (await client.post("/v1/extract", json=body)).status_code == 404


async def test_profile_default_scope_preserved_with_real_api_key(extraction_stack, monkeypatch):
    client, provider, owner, factory, tid, pid, app, principal = extraction_stack
    configure(provider, proposition("I no longer prefer dark mode."))
    wid = uuid4()
    async with owner.begin() as conn:
        await conn.execute(
            text("INSERT INTO workspaces(id,tenant_id,slug,name) VALUES (:w,:t,:s,:s)"),
            {"w": wid, "t": tid, "s": str(wid)},
        )
        await conn.execute(
            text("INSERT INTO workspace_members(workspace_id,principal_id) VALUES (:w,:p)"),
            {"w": wid, "p": pid},
        )
    app.dependency_overrides[get_current_principal] = lambda: replace(principal, scopes=("admin",))
    profile_reply = await client.post(
        "/v1/memory-profiles",
        json={
            "name": str(wid),
            "slug": str(wid),
            "reason": "extraction profile proof",
            "policy": {
                "include_private": True,
                "include_tenant": False,
                "include_public": False,
                "default_write_visibility": "workspace",
                "default_write_workspace_id": str(wid),
                "workspace_grants": [
                    {"workspace_id": str(wid), "can_read": True, "can_write": True}
                ],
            },
        },
    )
    assert profile_reply.status_code == 201, profile_reply.text
    profile = profile_reply.json()
    key_reply = await client.post(
        "/v1/admin/api-keys",
        json={
            "principal_id": str(pid),
            "tenant_id": str(tid),
            "scopes": ["read", "write"],
            "label": "extraction profile proof",
            "memory_profile_id": profile["id"],
        },
    )
    assert key_reply.status_code == 201, key_reply.text
    import engram.db as db_module
    from engram.auth import reset_principal_cache

    monkeypatch.setattr(db_module, "async_session_factory", factory)
    monkeypatch.setattr(settings, "auth_enabled", True)
    reset_principal_cache()
    app.dependency_overrides.pop(get_current_principal)
    headers = {"Authorization": "Bearer " + key_reply.json()["key"]}
    response = await client.post(
        "/v1/extract",
        json=request(
            mode="write_proposed",
            idempotency_key="profile-default",
        ),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt["workspace_id"] == str(wid)
    assert receipt["visibility"] == "workspace"
    assert receipt["memory_profile_revision_id"] == profile["active_revision_id"]
    assert receipt["candidates"][0]["outcome"] == "written", receipt
    assert (
        await client.get(f"/v1/extract/{receipt['run_id']}", headers=headers)
    ).status_code == 200
    assert (await client.post("/v1/extract", json=request())).status_code == 401
    assert (
        await client.post("/v1/extract", json=request(visibility="tenant"), headers=headers)
    ).status_code == 403
