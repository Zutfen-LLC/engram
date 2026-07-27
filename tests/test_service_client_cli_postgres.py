"""Owner-path service-client lifecycle and one-time-secret certification."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from engram.cli import _run_service_client
from engram.migrations import normalize_asyncpg_url
from engram.service_auth import parse_service_credential

pytestmark = [pytest.mark.asyncio, pytest.mark.service_provisioning_postgres]


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
                await owner.execute(
                    "DELETE FROM service_provisioning_events WHERE service_client_id = ANY($1)", ids
                )
                await owner.execute(
                    "DELETE FROM service_client_credentials WHERE service_client_id = ANY($1)", ids
                )
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
    assert (
        await proof["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_events "
            "WHERE service_client_id=$1 AND event_type IN ('service_client.created','service_credential.created')",
            client["id"],
        )
        == 2
    )

    rotate = _args("rotate-key", client=slug, label=None, expires_at=None, json=False)
    assert await _run_service_client(rotate, proof["url"]) == 0
    rotated = capsys.readouterr().out.strip().split("credential: ", 1)[1]
    assert rotated != credential
    assert (
        await proof["owner"].fetchval(
            "SELECT count(*) FROM service_client_credentials WHERE service_client_id=$1 AND status='active'",
            client["id"],
        )
        == 2
    )

    assert (
        await _run_service_client(_args("revoke-key", credential=parsed.key_id), proof["url"]) == 0
    )
    capsys.readouterr()
    assert (
        await proof["owner"].fetchval(
            "SELECT status FROM service_client_credentials WHERE key_id=$1", parsed.key_id
        )
        == "revoked"
    )
    assert await _run_service_client(_args("disable", client=slug), proof["url"]) == 0
    capsys.readouterr()
    assert (
        await proof["owner"].fetchval(
            "SELECT status FROM service_clients WHERE id=$1", client["id"]
        )
        == "disabled"
    )
    assert await _run_service_client(_args("enable", client=slug), proof["url"]) == 0
    capsys.readouterr()
    assert (
        await proof["owner"].fetchval(
            "SELECT status FROM service_clients WHERE id=$1", client["id"]
        )
        == "active"
    )
    assert (
        await proof["owner"].fetchval(
            "SELECT status FROM service_client_credentials WHERE key_id=$1", parsed.key_id
        )
        == "revoked"
    )


@pytest.mark.parametrize("as_json", (False, True))
async def test_cli_emits_each_new_credential_once_and_never_to_stderr(
    owner_cli_db, capsys, as_json: bool
) -> None:  # type: ignore[no-untyped-def]
    proof = owner_cli_db
    slug = f"cli-proof-once-{proof['tag']}-{int(as_json)}"
    create = _args(
        "create",
        slug=slug,
        display_name="One-time output",
        permission=None,
        json=as_json,
    )
    assert await _run_service_client(create, proof["url"]) == 0
    created = capsys.readouterr()
    credential = (
        json.loads(created.out)["credential"]
        if as_json
        else created.out.strip().split("credential: ", 1)[1]
    )
    assert created.out.count(credential) == 1
    assert created.err.find(credential) == -1

    assert (
        await _run_service_client(
            _args("rotate-key", client=slug, label=None, expires_at=None, json=as_json),
            proof["url"],
        )
        == 0
    )
    rotated = capsys.readouterr()
    next_credential = (
        json.loads(rotated.out)["credential"]
        if as_json
        else rotated.out.strip().split("credential: ", 1)[1]
    )
    assert rotated.out.count(next_credential) == 1
    assert rotated.out.find(credential) == -1
    assert rotated.err.find(credential) == -1
    assert rotated.err.find(next_credential) == -1

    rows = await proof["owner"].fetch(
        "SELECT key_id, secret_digest FROM service_client_credentials c "
        "JOIN service_clients s ON s.id=c.service_client_id WHERE s.slug=$1",
        slug,
    )
    assert len(rows) == 2
    assert all(row["key_id"] and row["secret_digest"] for row in rows)
    event_text = await proof["owner"].fetchval(
        "SELECT coalesce(string_agg(details::text, ''), '') FROM service_provisioning_events e "
        "JOIN service_clients s ON s.id=e.service_client_id WHERE s.slug=$1",
        slug,
    )
    assert str(event_text).find(credential) == -1
    assert str(event_text).find(next_credential) == -1


async def test_cli_repeat_and_unknown_targets_have_safe_operator_outcomes(
    owner_cli_db, capsys
) -> None:  # type: ignore[no-untyped-def]
    proof = owner_cli_db
    slug = f"cli-proof-repeat-{proof['tag']}"
    assert (
        await _run_service_client(
            _args("create", slug=slug, display_name="Repeat targets", permission=None, json=False),
            proof["url"],
        )
        == 0
    )
    credential = capsys.readouterr().out.strip().split("credential: ", 1)[1]
    key_id = parse_service_credential(credential).key_id
    assert await _run_service_client(_args("revoke-key", credential=key_id), proof["url"]) == 0
    capsys.readouterr()
    assert await _run_service_client(_args("revoke-key", credential=key_id), proof["url"]) == 0
    assert capsys.readouterr().out == "already revoked\n"
    for command, values in (
        ("revoke-key", {"credential": "missing-credential"}),
        ("disable", {"client": "missing-client"}),
        ("enable", {"client": "missing-client"}),
    ):
        assert await _run_service_client(_args(command, **values), proof["url"]) == 1
        failure = capsys.readouterr()
        assert failure.out == ""
        assert "ERROR: service" in failure.err
        assert failure.err.find(credential) == -1
    assert await _run_service_client(_args("disable", client=slug), proof["url"]) == 0
    capsys.readouterr()
    assert await _run_service_client(_args("disable", client=slug), proof["url"]) == 0
    assert capsys.readouterr().out == "already disabled\n"
    assert await _run_service_client(_args("enable", client=slug), proof["url"]) == 0
    capsys.readouterr()
    assert await _run_service_client(_args("enable", client=slug), proof["url"]) == 0
    assert capsys.readouterr().out == "already enabled\n"


@pytest.mark.parametrize(
    "create",
    (
        {"slug": "valid-slug", "display_name": "", "permission": None},
        {"slug": "valid-slug", "display_name": "  ", "permission": None},
        {"slug": "valid-slug", "display_name": " leading", "permission": None},
        {"slug": "valid-slug", "display_name": "trailing ", "permission": None},
        {"slug": "valid-slug", "display_name": "x" * 256, "permission": None},
        {"slug": "valid-slug", "display_name": "Valid", "permission": ["unknown"]},
        {
            "slug": "valid-slug",
            "display_name": "Valid",
            "permission": ["tenant.provision", "tenant.provision"],
        },
    ),
)
async def test_cli_rejects_invalid_create_input_before_connecting(create, capsys) -> None:  # type: ignore[no-untyped-def]
    args = _args("create", json=False, **create)
    assert await _run_service_client(args, "not-a-valid-database-url") == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR: invalid service client input\n"


async def test_cli_sanitizes_database_and_constraint_failures(owner_cli_db, capsys) -> None:  # type: ignore[no-untyped-def]
    proof = owner_cli_db
    args = _args(
        "create",
        slug=f"cli-proof-failure-{proof['tag']}",
        display_name="Safe failure",
        permission=None,
        json=False,
    )
    assert await _run_service_client(args, "not-a-valid-database-url") == 1
    failure = capsys.readouterr().err
    assert failure == "ERROR: service client operation failed\n"
    assert "asyncpg" not in failure.lower()
    assert "postgresql" not in failure.lower()

    assert await _run_service_client(args, proof["url"]) == 0
    capsys.readouterr()
    assert await _run_service_client(args, proof["url"]) == 1
    duplicate = capsys.readouterr().err
    assert duplicate == "ERROR: service client operation failed\n"


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


@pytest.mark.parametrize(
    "slug",
    ("Uppercase", "1leading-digit", "has space", "has.punctuation", "", "a" * 101),
)
async def test_cli_rejects_invalid_slug_without_creating_credentials_or_events(
    owner_cli_db, capsys, slug: str
) -> None:  # type: ignore[no-untyped-def]
    proof = owner_cli_db
    before_clients = await proof["owner"].fetchval("SELECT count(*) FROM service_clients")
    before_credentials = await proof["owner"].fetchval(
        "SELECT count(*) FROM service_client_credentials"
    )
    before_events = await proof["owner"].fetchval(
        "SELECT count(*) FROM service_provisioning_events"
    )

    assert (
        await _run_service_client(
            _args("create", slug=slug, display_name="Invalid slug", permission=None, json=False),
            proof["url"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR: invalid service client input\n"
    assert await proof["owner"].fetchval("SELECT count(*) FROM service_clients") == before_clients
    assert (
        await proof["owner"].fetchval("SELECT count(*) FROM service_client_credentials")
        == before_credentials
    )
    assert (
        await proof["owner"].fetchval("SELECT count(*) FROM service_provisioning_events")
        == before_events
    )


@pytest.mark.parametrize("expires_at", ("not-a-timestamp", "2024-02-30T12:00:00Z"))
async def test_cli_rejects_invalid_expiry_without_committing_credential_or_event(
    owner_cli_db, capsys, expires_at: str
) -> None:  # type: ignore[no-untyped-def]
    proof = owner_cli_db
    slug = f"cli-proof-expiry-{proof['tag']}"
    assert (
        await _run_service_client(
            _args("create", slug=slug, display_name="Expiry input", permission=None, json=False),
            proof["url"],
        )
        == 0
    )
    capsys.readouterr()
    client_id = await proof["owner"].fetchval("SELECT id FROM service_clients WHERE slug=$1", slug)
    assert client_id is not None
    before_credentials = await proof["owner"].fetchval(
        "SELECT count(*) FROM service_client_credentials WHERE service_client_id=$1", client_id
    )
    before_events = await proof["owner"].fetchval(
        "SELECT count(*) FROM service_provisioning_events WHERE service_client_id=$1", client_id
    )

    assert (
        await _run_service_client(
            _args("rotate-key", client=slug, label=None, expires_at=expires_at, json=False),
            proof["url"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR: invalid service client input\n"
    assert (
        await proof["owner"].fetchval(
            "SELECT count(*) FROM service_client_credentials WHERE service_client_id=$1", client_id
        )
        == before_credentials
    )
    assert (
        await proof["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_events WHERE service_client_id=$1", client_id
        )
        == before_events
    )


async def test_cli_rejects_oversized_rotate_label_without_minting_a_credential(
    owner_cli_db, capsys
) -> None:  # type: ignore[no-untyped-def]
    proof = owner_cli_db
    slug = f"cli-proof-label-{proof['tag']}"
    assert (
        await _run_service_client(
            _args("create", slug=slug, display_name="Label input", permission=None, json=False),
            proof["url"],
        )
        == 0
    )
    capsys.readouterr()
    client_id = await proof["owner"].fetchval("SELECT id FROM service_clients WHERE slug=$1", slug)
    assert client_id is not None
    before_credentials = await proof["owner"].fetchval(
        "SELECT count(*) FROM service_client_credentials WHERE service_client_id=$1", client_id
    )
    before_events = await proof["owner"].fetchval(
        "SELECT count(*) FROM service_provisioning_events WHERE service_client_id=$1", client_id
    )

    assert (
        await _run_service_client(
            _args("rotate-key", client=slug, label="x" * 256, expires_at=None, json=False),
            proof["url"],
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "ERROR: invalid service client input\n"
    assert (
        await proof["owner"].fetchval(
            "SELECT count(*) FROM service_client_credentials WHERE service_client_id=$1", client_id
        )
        == before_credentials
    )
    assert (
        await proof["owner"].fetchval(
            "SELECT count(*) FROM service_provisioning_events WHERE service_client_id=$1", client_id
        )
        == before_events
    )
