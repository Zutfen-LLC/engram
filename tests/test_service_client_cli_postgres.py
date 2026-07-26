"""Owner-path service-client lifecycle and one-time-secret certification."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from engram.cli import _run_service_client
from engram.migrations import normalize_asyncpg_url
from engram.service_auth import parse_service_credential

pytestmark = pytest.mark.asyncio


async def _connect(url: str):  # type: ignore[no-untyped-def]
    import asyncpg

    return await asyncpg.connect(normalize_asyncpg_url(url))


@pytest.fixture
async def owner_cli_db() -> AsyncIterator[dict[str, Any]]:
    owner_url = os.getenv("ENGRAM_OWNER_DATABASE_URL") or os.getenv("ENGRAM_DATABASE_URL")
    if not owner_url:
        pytest.skip("requires an owner PostgreSQL URL")
    try:
        owner = await _connect(owner_url)
    except Exception:
        pytest.skip("requires migrated PostgreSQL")
    tag = uuid.uuid4().hex[:16]
    try:
        yield {"owner": owner, "url": owner_url, "tag": tag}
    finally:
        client_ids = await owner.fetch(
            "SELECT id FROM service_clients WHERE slug LIKE $1", f"cli-proof-%-{tag}%"
        )
        ids = [row["id"] for row in client_ids]
        if ids:
            async with owner.transaction():
                await owner.execute("DELETE FROM service_provisioning_events WHERE service_client_id = ANY($1)", ids)
                await owner.execute("DELETE FROM service_client_credentials WHERE service_client_id = ANY($1)", ids)
                await owner.execute("DELETE FROM service_clients WHERE id = ANY($1)", ids)
        await owner.close()


def _args(command: str, **values: object) -> argparse.Namespace:
    return argparse.Namespace(service_client_command=command, **values)


async def test_cli_create_rotate_revoke_disable_enable_preserves_secret_boundary(
    owner_cli_db, capsys
) -> None:  # type: ignore[no-untyped-def]
    proof = owner_cli_db
    slug = f"cli-proof-control-{proof['tag']}"
    create = _args(
        "create",
        slug=slug,
        display_name="Control Plane",
        permission=None,
        json=False,
    )
    assert await _run_service_client(create, proof["url"]) == 0
    output = capsys.readouterr()
    credential = output.out.strip().split("credential: ", 1)[1]
    parsed = parse_service_credential(credential)
    client = await proof["owner"].fetchrow("SELECT id FROM service_clients WHERE slug=$1", slug)
    assert client is not None
    rows = await proof["owner"].fetch(
        "SELECT key_id, secret_digest, status FROM service_client_credentials WHERE service_client_id=$1",
        client["id"],
    )
    assert len(rows) == 1
    assert rows[0]["key_id"] == parsed.key_id
    assert credential not in str(dict(rows[0]))
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_provisioning_events "
        "WHERE service_client_id=$1 AND event_type IN ('service_client.created','service_credential.created')",
        client["id"],
    ) == 2

    rotate = _args("rotate-key", client=slug, label=None, expires_at=None, json=False)
    assert await _run_service_client(rotate, proof["url"]) == 0
    rotated = capsys.readouterr().out.strip().split("credential: ", 1)[1]
    assert rotated != credential
    assert await proof["owner"].fetchval(
        "SELECT count(*) FROM service_client_credentials WHERE service_client_id=$1 AND status='active'",
        client["id"],
    ) == 2

    assert await _run_service_client(_args("revoke-key", credential=parsed.key_id), proof["url"]) == 0
    capsys.readouterr()
    assert await proof["owner"].fetchval(
        "SELECT status FROM service_client_credentials WHERE key_id=$1", parsed.key_id
    ) == "revoked"
    assert await _run_service_client(_args("disable", client=slug), proof["url"]) == 0
    capsys.readouterr()
    assert await proof["owner"].fetchval("SELECT status FROM service_clients WHERE id=$1", client["id"]) == "disabled"
    assert await _run_service_client(_args("enable", client=slug), proof["url"]) == 0
    capsys.readouterr()
    assert await proof["owner"].fetchval("SELECT status FROM service_clients WHERE id=$1", client["id"]) == "active"
    assert await proof["owner"].fetchval(
        "SELECT status FROM service_client_credentials WHERE key_id=$1", parsed.key_id
    ) == "revoked"


async def test_cli_rejects_bad_display_name_before_connecting(capsys) -> None:  # type: ignore[no-untyped-def]
    args = _args(
        "create",
        slug="valid-slug",
        display_name="   ",
        permission=None,
        json=False,
    )
    assert await _run_service_client(args, "not-a-valid-database-url") == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR: invalid service client input\n"
