"""Verify capture transactions against PostgreSQL, including write rejection."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.config import settings
from engram.models import MemoryItem
from evals.admission import snapshot
from evals.admission.schema import Sampling


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "app"])
async def test_snapshot_is_read_only_and_rejects_accidental_writes(monkeypatch, role):
    engine = create_async_engine(settings.database_url)
    capture_engine = None
    item_id = None
    tenant = principal = None
    try:
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT t.id, p.id FROM tenants t "
                            "JOIN principals p ON p.tenant_id=t.id "
                            "WHERE t.slug='default' AND p.name='admin'"
                        )
                    )
                ).first()
        except Exception:
            pytest.skip("requires a live PostgreSQL with the v2 schema")
        assert row is not None
        tenant, principal = (uuid.UUID(str(value)) for value in row)
        item_id = uuid.uuid4()
        async with AsyncSession(engine) as session, session.begin():
            await session.execute(
                text(
                    "SELECT set_config('app.tenant_id', :t, true), "
                    "set_config('app.principal_id', :p, true)"
                ),
                {"t": str(tenant), "p": str(principal)},
            )
            session.add(
                MemoryItem(
                    id=item_id,
                    tenant_id=tenant,
                    principal_id=principal,
                    content="Synthetic admission capture test.",
                    content_hash="sha256:" + item_id.hex * 2,
                    kind="fact",
                    source_type="manual",
                    review_status="proposed",
                    memory_confidence=0.8,
                    source_trust=0.9,
                    source_confidence_prior=0.8,
                    authority=10,
                )
            )
        capture_engine = create_async_engine(
            os.environ["ENGRAM_APP_DATABASE_URL"] if role == "app" else settings.database_url
        )
        statements = []

        @event.listens_for(capture_engine.sync_engine, "before_cursor_execute")
        def observe(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        monkeypatch.setattr(snapshot, "create_async_engine", lambda *a, **kw: capture_engine)
        kwargs = dict(
            url=settings.database_url,
            tenant=tenant,
            principal=principal,
            key=b"x" * 32,
            code_sha="0" * 40,
            sampling=Sampling(
                selection_method="census", selection_seed="test", strata=(), per_stratum=1
            ),
            dataset_id="test-snapshot",
            dataset_version="1",
        )
        data = await snapshot.capture(**kwargs)
        assert data.manifest.sample_count >= 1
        assert data.manifest.sample_count == data.manifest.eligible_population_count
        assert all(s.content is None and s.label is None for s in data.samples)
        assert statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        assert all(sql.lstrip().upper().startswith(("SELECT", "SET")) for sql in statements)

        async def try_write(session, tenant_id):
            await session.execute(
                text("UPDATE tenant_config SET auto_promote_enabled=false WHERE false")
            )

        monkeypatch.setattr(snapshot, "_config", try_write)
        with pytest.raises(Exception, match="read-only transaction"):
            await snapshot.capture(**kwargs)
    finally:
        if item_id is not None:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "SELECT set_config('app.tenant_id', :t, true), "
                        "set_config('app.principal_id', :p, true)"
                    ),
                    {"t": str(tenant), "p": str(principal)},
                )
                await connection.execute(
                    text("DELETE FROM memory_items WHERE id=:id"), {"id": item_id}
                )
        if capture_engine is not None:
            await capture_engine.dispose()
        await engine.dispose()
