"""Real PostgreSQL upgrade, reapply, linkage, immutability, and downgrade proofs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from engram.extraction import digest
from engram.migrations import normalize_asyncpg_url


async def test_extraction_migration_rollback_preserves_memory_and_policy():
    url = os.environ.get("ENGRAM_OWNER_DATABASE_URL")
    if not url:
        pytest.skip("requires a live PostgreSQL with the v2 schema")
    conn = await asyncpg.connect(normalize_asyncpg_url(url))
    schema = f"extraction_{uuid4().hex}"
    t, p, w, item, ingest, run, candidate = (uuid4() for _ in range(7))
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute("""
            CREATE TABLE tenants(id uuid PRIMARY KEY);
            CREATE TABLE principals(id uuid PRIMARY KEY, tenant_id uuid, type text,
                                    UNIQUE(tenant_id,id));
            CREATE TABLE workspaces(id uuid PRIMARY KEY, tenant_id uuid, UNIQUE(tenant_id,id));
            CREATE TABLE workspace_members(workspace_id uuid, principal_id uuid);
            CREATE TABLE memory_items(id uuid PRIMARY KEY, tenant_id uuid, principal_id uuid,
                workspace_id uuid, content_hash text, review_status text, authority int,
                retention_confidence real);
            CREATE TABLE candidate_ingests(id uuid PRIMARY KEY, tenant_id uuid, principal_id uuid,
                workspace_id uuid, content_hash text);
        """)
        await conn.execute("INSERT INTO tenants VALUES($1)", t)
        await conn.execute("INSERT INTO principals VALUES($1,$2,'agent')", p, t)
        await conn.execute("INSERT INTO workspaces VALUES($1,$2)", w, t)
        await conn.execute("INSERT INTO workspace_members VALUES($1,$2)", w, p)
        await conn.execute(
            "INSERT INTO memory_items VALUES($1,$2,$3,$4,'hash','active',40,NULL)",
            item,
            t,
            p,
            w,
        )
        await conn.execute(
            "INSERT INTO candidate_ingests VALUES($1,$2,$3,$4,'hash')",
            ingest,
            t,
            p,
            w,
        )
        before = await conn.fetchrow("SELECT * FROM memory_items")
        upgrade = Path("migrations/036_extraction_receipts.sql").read_text()
        await conn.execute(upgrade)
        await conn.execute(upgrade)
        assert await conn.fetchrow("SELECT * FROM memory_items") == before
        for table in ("extraction_runs", "extraction_item_links"):
            assert await conn.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid=$1::regclass",
                table,
            )
        receipt = {
            "schema_version": "engram.extraction.v1",
            "run_id": str(run),
            "tenant_id": str(t),
            "principal_id": str(p),
            "workspace_id": str(w),
            "mode": "write_proposed",
            "candidates": [
                {
                    "candidate_id": str(candidate),
                    "memory_item_id": str(item),
                    "ingest_id": str(ingest),
                    "content_hash": "hash",
                }
            ],
        }
        await conn.execute(
            "INSERT INTO extraction_runs(id,tenant_id,principal_id,workspace_id,idempotency_key,"
            "request_hash,receipt,receipt_hash) VALUES($1,$2,$3,$4,'retry',$5,$6,$5)",
            run,
            t,
            p,
            w,
            digest(receipt),
            json.dumps(receipt),
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("UPDATE extraction_runs SET receipt='{}' WHERE id=$1", run)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO extraction_item_links VALUES($1,$2,$3,$4,NULL,$5,$6)",
                run,
                candidate,
                t,
                p,
                item,
                ingest,
            )
        await conn.execute(
            "INSERT INTO extraction_item_links VALUES($1,$2,$3,$4,$5,$6,$7)",
            run,
            candidate,
            t,
            p,
            w,
            item,
            ingest,
        )
        await conn.execute(Path("migrations/downgrades/036_extraction_receipts.sql").read_text())
        assert await conn.fetchval("SELECT to_regclass('extraction_runs')") is None
        assert await conn.fetchrow("SELECT * FROM memory_items") == before
        await conn.execute(upgrade)
        assert await conn.fetchrow("SELECT * FROM memory_items") == before
    finally:
        await conn.execute("RESET search_path")
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()
