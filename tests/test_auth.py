# ruff: noqa: E501
"""Tests for API key auth, scope enforcement, workspace membership, and admin endpoints.

Uses an in-memory SQLite with manually-created tables (the same pattern as
test_items.py) so the full dependency chain — including get_session RLS context
and get_current_principal — runs without a live Postgres.

The auth module resolves the caller via a lazy ``_get_session_factory()``
accessor. Tests monkeypatch that accessor so ``get_current_principal`` reads
from the test SQLite DB, not the module-level Postgres engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from engram.api.app import create_app
from engram.auth import (
    DIGEST_ALGORITHM,
    ParsedApiKey,
    check_workspace_membership,
    digest_api_key_secret,
    generate_api_key,
    hash_api_key,
    parse_api_key,
    reset_principal_cache,
    verify_api_key,
    verify_api_key_secret,
)
from engram.db import get_session

CREATE_STATEMENTS = [
    """
    CREATE TABLE tenants (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        slug TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workspaces (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        slug TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE principals (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        internal_key TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE workspace_members (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE api_keys (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        principal_id TEXT,
        key_hash TEXT,
        key_id TEXT,
        secret_digest TEXT,
        digest_algorithm TEXT,
        scopes TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE memory_kinds (
        tenant_id TEXT NOT NULL,
        name TEXT NOT NULL,
        display_name TEXT NOT NULL,
        description TEXT,
        is_builtin INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        singleton INTEGER NOT NULL DEFAULT 0,
        stays_in_recall_when_disputed INTEGER NOT NULL DEFAULT 0,
        requires_review INTEGER NOT NULL DEFAULT 0,
        auto_promote_from_inferred INTEGER NOT NULL DEFAULT 0,
        default_importance REAL,
        sort_order INTEGER NOT NULL DEFAULT 100,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, name)
    )
    """,
    """
    CREATE TABLE tenant_config (
        id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        config_version TEXT,
        weight_importance REAL,
        weight_source_trust REAL,
        weight_memory_confidence REAL,
        weight_recency REAL,
        weight_verified REAL,
        auto_promote_enabled INTEGER,
        auto_promote_confidence_threshold REAL,
        auto_promote_min_age_hours INTEGER,
        auto_promote_evidence_enabled INTEGER,
        auto_promote_evidence_threshold REAL,
        max_pinned_tokens INTEGER,
        stale_after_days INTEGER,
        startup_recall_penalty_threshold INTEGER,
        startup_recall_penalty_factor REAL,
        feedback_daily_limit INTEGER,
        trust_manual_user REAL,
        trust_manual_agent REAL,
        trust_import REAL,
        trust_extraction REAL,
        trust_sync_turn REAL,
        trust_pre_compress REAL,
        trust_session_end REAL,
        confidence_manual_user REAL,
        confidence_manual_agent REAL,
        confidence_import REAL,
        confidence_extraction REAL,
        confidence_sync_turn REAL,
        confidence_pre_compress REAL,
        confidence_session_end REAL,
        active INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,

]


def _scopes_to_sql(scopes: list[str]) -> str:
    return ",".join(scopes)


@pytest.fixture()
async def session_factory(tmp_path: Path):
    db_path = tmp_path / "engram_auth.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        for stmt in CREATE_STATEMENTS:
            await conn.exec_driver_sql(stmt)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_principal_cache():
    """The principal cache is module-level; clear it between tests."""
    reset_principal_cache()


@pytest.fixture()
async def seeded(session_factory):
    async with session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO tenants (id, name, slug, created_at) "
                "VALUES ('00000000-0000-0000-0000-000000000001', 'Default', 'default', '2026-01-01')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO principals (id, tenant_id, name, type, created_at) "
                "VALUES ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'admin', 'admin', '2026-01-01')"
            )
        )
        await session.commit()
    return session_factory


@pytest.fixture()
def patch_auth_factory(seeded, monkeypatch):
    """Point auth._get_session_factory at the test DB so the dependency
    chain resolves principals from SQLite, not the module Postgres engine."""
    import engram.auth as auth_mod

    monkeypatch.setattr(auth_mod, "_get_session_factory", lambda: seeded)
    return seeded


@pytest.fixture()
def make_client(patch_auth_factory):
    """Factory: builds a client with a given auth_enabled setting."""

    def _build(*, auth_enabled: bool) -> AsyncClient:
        from engram.config import settings as _settings

        _settings.auth_enabled = auth_enabled
        app = create_app()

        async def override_get_session() -> AsyncIterator[AsyncSession]:
            async with patch_auth_factory() as session:
                yield session

        app.dependency_overrides[get_session] = override_get_session
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")

    return _build


async def _seed_api_key(
    factory, *, scopes: list[str], tenant_id: str = "00000000-0000-0000-0000-000000000001"
) -> str:
    """Seed a LEGACY bcrypt key (eng_<random>, key_id NULL) and return plaintext."""
    plaintext = generate_api_key_legacy()
    key_hash = hash_api_key(plaintext)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, :kh, :sc, :lbl, '2026-01-01', NULL)"
            ),
            {
                "id": f"k-{scopes[0]}",
                "tid": tenant_id,
                "pid": "p-admin",
                "kh": key_hash,
                "sc": _scopes_to_sql(scopes),
                "lbl": "test",
            },
        )
        await session.commit()
    return plaintext


def generate_api_key_legacy() -> str:
    """A pre-AUD-003 style key: eng_ + url-safe random (no key_id)."""
    import secrets

    return "eng_" + secrets.token_urlsafe(32)


async def _seed_new_api_key(
    factory,
    *,
    scopes: list[str],
    tenant_id: str = "00000000-0000-0000-0000-000000000001",
    principal_id: str = "p-admin",
    label: str = "test-new",
    key_id_override: str | None = None,
) -> tuple[str, str]:
    """Seed a NEW-format key (eng_<key_id>_<secret>) and return (plaintext, key_id)."""
    plaintext = generate_api_key()
    parsed = parse_api_key(plaintext)
    assert parsed.key_id is not None
    key_id = key_id_override or parsed.key_id
    digest = digest_api_key_secret(parsed.secret)
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys "
                "  (id, tenant_id, principal_id, key_hash, key_id, secret_digest, "
                "   digest_algorithm, scopes, label, created_at, revoked_at) "
                "VALUES (:id, :tid, :pid, NULL, :kid, :sd, :da, :sc, :lbl, '2026-01-01', NULL)"
            ),
            {
                "id": f"kn-{label}",
                "tid": tenant_id,
                "pid": principal_id,
                "kid": key_id,
                "sd": digest,
                "da": DIGEST_ALGORITHM,
                "sc": _scopes_to_sql(scopes),
                "lbl": label,
            },
        )
        await session.commit()
    return plaintext, key_id


# === Unit tests for key gen/hash/verify ===


def test_generate_api_key_format():
    key = generate_api_key()
    assert key.startswith("eng_")
    assert len(key) > 20


def test_hash_and_verify_api_key_roundtrip():
    key = generate_api_key()
    h = hash_api_key(key)
    assert verify_api_key(key, h) is True
    assert verify_api_key("eng_wrong", h) is False


def test_verify_api_key_garbage_hash():
    assert verify_api_key("eng_x", "not-a-real-hash") is False


# === New-format key generation / parsing ===


def test_generate_api_key_shape():
    key = generate_api_key()
    assert key.startswith("eng_")
    # Shape: eng_<key_id>_<secret> — exactly one underscore after the prefix.
    rest = key[len("eng_"):]
    key_id, sep, secret = rest.partition("_")
    assert sep == "_"
    assert key_id  # non-empty
    assert secret  # non-empty
    # key_id is base62 (no separators), so no further underscores before secret.
    assert "_" not in key_id


def test_generate_api_key_key_id_is_base62():
    import string

    allowed = set(string.digits + string.ascii_lowercase + string.ascii_uppercase)
    for _ in range(50):
        parsed = parse_api_key(generate_api_key())
        assert parsed.is_legacy is False
        assert parsed.key_id is not None
        assert set(parsed.key_id) <= allowed


def test_generate_api_keys_are_unique():
    keys = {generate_api_key() for _ in range(200)}
    assert len(keys) == 200


def test_parse_api_key_new_format():
    parsed = parse_api_key("eng_abcDEF123_some-secret_with_underscores")
    assert parsed == ParsedApiKey(
        key_id="abcDEF123", secret="some-secret_with_underscores", is_legacy=False
    )


def test_parse_api_key_legacy_format():
    parsed = parse_api_key("eng_just-a-random-token-no-inner-structure")
    assert parsed.is_legacy is True
    assert parsed.key_id is None
    # A legacy token whose random segment happens to contain no underscore
    # parses cleanly as legacy.
    assert parsed.secret == "just-a-random-token-no-inner-structure"


def test_parse_api_key_legacy_with_underscore_parses_as_new_candidate():
    # A legacy key whose random contains '_' parses as a new-format candidate;
    # the resolver handles the resulting key_id miss by falling back to bcrypt.
    parsed = parse_api_key("eng_legacysegment_rest-of-random")
    assert parsed.is_legacy is False
    assert parsed.key_id == "legacysegment"


def test_parse_api_key_rejects_missing_prefix():
    with pytest.raises(ValueError):
        parse_api_key("noteng_abc_def")


def test_parse_api_key_rejects_bare_prefix():
    with pytest.raises(ValueError):
        parse_api_key("eng_")


def test_parse_api_key_treats_leading_separator_as_legacy():
    parsed = parse_api_key("eng__secret")
    assert parsed == ParsedApiKey(key_id=None, secret="_secret", is_legacy=True)


def test_parse_api_key_treats_trailing_separator_as_legacy():
    parsed = parse_api_key("eng_kid_")
    assert parsed == ParsedApiKey(key_id=None, secret="kid_", is_legacy=True)


# === New-format digest helpers ===


def test_digest_and_verify_secret_roundtrip():
    digest = digest_api_key_secret("a-high-entropy-secret")
    assert digest != "a-high-entropy-secret"
    assert verify_api_key_secret("a-high-entropy-secret", digest) is True
    assert verify_api_key_secret("wrong-secret", digest) is False


def test_verify_api_key_secret_constant_time_garbage():
    assert verify_api_key_secret("x", "not-a-hex-digest") is False
    assert verify_api_key_secret("x", "") is False


# === Health exempt + auth-disabled flow ===


async def test_health_exempt_no_auth(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
    finally:
        await c.aclose()


async def test_health_works_with_auth_enabled(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.get("/health")
        assert resp.status_code == 200
    finally:
        await c.aclose()


async def test_admin_works_when_auth_disabled(make_client):
    c = make_client(auth_enabled=False)
    try:
        resp = await c.post(
            "/v1/admin/tenants", json={"name": "Acme", "slug": "acme"}
        )
        assert resp.status_code == 201
        assert resp.json()["slug"] == "acme"
    finally:
        await c.aclose()


async def test_create_principal_when_auth_disabled(make_client):
    c = make_client(auth_enabled=False)
    try:
        resp = await c.post(
            "/v1/admin/principals",
            json={"tenant_id": "00000000-0000-0000-0000-000000000001", "name": "bot-1", "type": "agent"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "bot-1"
    finally:
        await c.aclose()


# === Auth enabled ===


async def test_missing_token_returns_401(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants", json={"name": "X", "slug": "x"}
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_invalid_token_returns_401(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "X", "slug": "x"},
            headers={"Authorization": "Bearer eng_totallybogus"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_non_bearer_scheme_rejected(make_client):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "X", "slug": "x"},
            headers={"Authorization": "Basic abc123"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_valid_token_admin_scope(make_client, patch_auth_factory):
    key = await _seed_api_key(patch_auth_factory, scopes=["read", "write", "admin"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "ViaKey", "slug": "viakey"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "ViaKey"
    finally:
        await c.aclose()


async def test_valid_token_missing_scope_403(make_client, patch_auth_factory):
    key = await _seed_api_key(patch_auth_factory, scopes=["read"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "NoScope", "slug": "noscope"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 403
    finally:
        await c.aclose()


async def test_revoked_key_rejected(make_client, patch_auth_factory):
    key = await _seed_api_key(patch_auth_factory, scopes=["admin"])
    async with patch_auth_factory() as session:
        await session.execute(
            text("UPDATE api_keys SET revoked_at = '2026-01-02' WHERE label = 'test'")
        )
        await session.commit()
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "Revoked", "slug": "revoked"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


# === New-format key storage ===


async def test_new_key_stores_indexed_fields_not_plaintext(patch_auth_factory):
    plaintext, key_id = await _seed_new_api_key(
        patch_auth_factory, scopes=["read", "write", "admin"]
    )
    async with patch_auth_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT key_id, secret_digest, digest_algorithm, key_hash "
                    "FROM api_keys WHERE label = 'test-new'"
                )
            )
        ).one()
    assert row.key_id == key_id
    assert row.secret_digest == digest_api_key_secret(parse_api_key(plaintext).secret)
    assert row.digest_algorithm == DIGEST_ALGORITHM
    # No bcrypt hash for new-format keys, and no plaintext anywhere.
    assert row.key_hash is None
    assert plaintext not in (row.secret_digest or "")
    assert key_id in plaintext  # the printed key embeds the key_id


# === New-format key auth ===


async def test_new_format_key_authenticates_with_correct_scopes(make_client, patch_auth_factory):
    plaintext, _ = await _seed_new_api_key(
        patch_auth_factory, scopes=["read", "write", "admin"]
    )
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "NewKey", "slug": "newkey"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "NewKey"
    finally:
        await c.aclose()


async def test_new_format_key_missing_scope_403(make_client, patch_auth_factory):
    plaintext, _ = await _seed_new_api_key(patch_auth_factory, scopes=["read"])
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "NoScope", "slug": "noscope"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert resp.status_code == 403
    finally:
        await c.aclose()


async def test_new_format_key_wrong_secret_returns_401(make_client, patch_auth_factory):
    plaintext, _ = await _seed_new_api_key(patch_auth_factory, scopes=["admin"])
    # Same key_id, tampered secret.
    parsed = parse_api_key(plaintext)
    tampered = f"eng_{parsed.key_id}_a-completely-wrong-secret"
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "X", "slug": "x"},
            headers={"Authorization": f"Bearer {tampered}"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_new_format_unknown_key_id_returns_401(make_client, patch_auth_factory):
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "X", "slug": "x"},
            # Well-formed new-format token whose key_id is not in the DB.
            headers={"Authorization": "Bearer eng_0000000000000000000000_unknownsecret"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


async def test_new_format_revoked_key_returns_401(make_client, patch_auth_factory):
    plaintext, _ = await _seed_new_api_key(patch_auth_factory, scopes=["admin"])
    # Reset the cache so the revocation check hits the DB.
    reset_principal_cache()
    async with patch_auth_factory() as session:
        await session.execute(
            text("UPDATE api_keys SET revoked_at = '2026-01-02' WHERE label = 'test-new'")
        )
        await session.commit()
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "Rev", "slug": "rev"},
            headers={"Authorization": f"Bearer {plaintext}"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


# === O(1) behavior regression ===


async def _resolve_direct(plaintext: str, *, path: str = "/v1/admin/tenants"):
    """Call get_current_principal directly with auth enabled."""
    from types import SimpleNamespace

    import engram.auth as auth_mod
    from engram.config import settings as _settings

    _settings.auth_enabled = True
    request = SimpleNamespace(url=SimpleNamespace(path=path))
    creds = SimpleNamespace(scheme="Bearer", credentials=plaintext)
    return await auth_mod.get_current_principal(request, creds)


async def test_new_format_auth_is_o1_single_lookup_no_bcrypt(
    patch_auth_factory, monkeypatch
):
    """A valid new-format key resolves with ONE indexed query and ZERO bcrypt calls.

    Regression guard: this fails if the implementation reverts to the O(n·bcrypt)
    scan for new-format keys.
    """
    from sqlalchemy import event

    import engram.auth as auth_mod

    plaintext, _ = await _seed_new_api_key(
        patch_auth_factory, scopes=["read", "write", "admin"]
    )
    reset_principal_cache()

    sync_engine = patch_auth_factory().bind.sync_engine
    executed: list[str] = []

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, *args, **kwargs):  # noqa: ANN001, ARG001
        executed.append(statement)

    bcrypt_calls = {"n": 0}
    real_verify = auth_mod.verify_api_key

    def _spy(plaintext_arg, key_hash):  # noqa: ANN001
        bcrypt_calls["n"] += 1
        return real_verify(plaintext_arg, key_hash)

    monkeypatch.setattr(auth_mod, "verify_api_key", _spy)

    try:
        principal = await _resolve_direct(plaintext)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)

    assert principal is not None
    assert principal.scopes == ("read", "write", "admin")
    # Exactly one DB query — the indexed key_id lookup — and it filters on key_id.
    assert len(executed) == 1, f"expected 1 query, got {executed}"
    sql = executed[0].lower()
    assert "from api_keys" in sql
    assert "key_id =" in sql or "key_id=" in sql
    # The new-format path never invokes bcrypt.
    assert bcrypt_calls["n"] == 0


async def test_new_format_auth_does_not_load_all_rows(patch_auth_factory, monkeypatch):
    """Multiple keys exist; a new-format auth still runs exactly one key_id query."""
    from sqlalchemy import event

    # Two extra legacy keys + one extra new key in the table.
    await _seed_api_key(patch_auth_factory, scopes=["read"])
    await _seed_api_key(patch_auth_factory, scopes=["write"])
    await _seed_new_api_key(patch_auth_factory, scopes=["read"], label="other-new")
    plaintext, _ = await _seed_new_api_key(
        patch_auth_factory, scopes=["admin"], label="target-new"
    )
    reset_principal_cache()

    sync_engine = patch_auth_factory().bind.sync_engine
    executed: list[str] = []

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, *args, **kwargs):  # noqa: ANN001, ARG001
        executed.append(statement)

    try:
        principal = await _resolve_direct(plaintext)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)

    assert principal is not None
    assert principal.scopes == ("admin",)
    assert len(executed) == 1, f"expected 1 query, got {executed}"


# === Legacy compatibility ===


async def test_legacy_bcrypt_key_still_authenticates(make_client, patch_auth_factory):
    # A real pre-AUD-003 key: eng_ + url-safe random, stored as a bcrypt hash.
    key = await _seed_api_key(patch_auth_factory, scopes=["read", "write", "admin"])
    # Sanity: the legacy key may contain underscores in its random segment.
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "Legacy", "slug": "legacy"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Legacy"
    finally:
        await c.aclose()


async def test_legacy_bcrypt_key_with_underscore_authenticates(
    make_client, patch_auth_factory
):
    """A legacy key whose random contains '_' parses as new-format but must still
    resolve via the bcrypt fallback (key_id miss -> legacy scan)."""
    import secrets

    # Deterministic underscore placement: a fixed prefix guarantees an internal
    # '_' regardless of the random tail (token_urlsafe can produce a run with no
    # '-'/'_', which would otherwise make the precondition flaky ~26% of the time).
    legacy = "eng_legacykeyprefix_" + secrets.token_urlsafe(32)
    assert "_" in legacy[len("eng_"):]  # precondition: has an internal underscore
    key_hash = hash_api_key(legacy)
    async with patch_auth_factory() as session:
        await session.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, principal_id, key_hash, scopes, label, created_at, revoked_at) "
                "VALUES ('k-under', '00000000-0000-0000-0000-000000000001', 'p-admin', :kh, 'admin', 'leg-under', '2026-01-01', NULL)"
            ),
            {"kh": key_hash},
        )
        await session.commit()
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "LegU", "slug": "legu"},
            headers={"Authorization": f"Bearer {legacy}"},
        )
        assert resp.status_code == 201
    finally:
        await c.aclose()


async def test_revoked_legacy_key_rejected(make_client, patch_auth_factory):
    key = await _seed_api_key(patch_auth_factory, scopes=["admin"], )
    async with patch_auth_factory() as session:
        await session.execute(
            text("UPDATE api_keys SET revoked_at = '2026-01-02' WHERE label = 'test'")
        )
        await session.commit()
    c = make_client(auth_enabled=True)
    try:
        resp = await c.post(
            "/v1/admin/tenants",
            json={"name": "Rev", "slug": "rev"},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 401
    finally:
        await c.aclose()


# === Principal cache (new-format keys) ===


async def test_cache_hit_avoids_repeated_lookup(patch_auth_factory, monkeypatch):
    from sqlalchemy import event

    plaintext, _ = await _seed_new_api_key(
        patch_auth_factory, scopes=["read", "write", "admin"]
    )
    reset_principal_cache()

    sync_engine = patch_auth_factory().bind.sync_engine
    executed: list[str] = []

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, *args, **kwargs):  # noqa: ANN001, ARG001
        executed.append(statement)

    try:
        first = await _resolve_direct(plaintext)
        n_after_first = len(executed)
        assert first is not None
        # Second resolution within TTL -> cache hit, no additional DB query.
        second = await _resolve_direct(plaintext)
        assert second is not None
        assert second == first
        assert len(executed) == n_after_first, "second auth should hit the cache"
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture)


async def test_failed_auth_not_cached(patch_auth_factory):
    from fastapi import HTTPException

    plaintext, _ = await _seed_new_api_key(patch_auth_factory, scopes=["admin"])
    parsed = parse_api_key(plaintext)
    tampered = f"eng_{parsed.key_id}_wrong-secret"

    reset_principal_cache()
    with pytest.raises(HTTPException) as exc1:
        await _resolve_direct(tampered)
    assert exc1.value.status_code == 401
    # The failed secret must not have been cached: a second attempt with the
    # CORRECT secret still resolves (proving nothing stale was stored for the
    # key_id). If the wrong secret had been cached as a denial, this would be
    # ambiguous; here we assert the cache only ever holds successful principals.
    ok = await _resolve_direct(plaintext)
    assert ok is not None
    assert ok.scopes == ("admin",)


async def test_revoked_key_rejected_after_cache_expiry(patch_auth_factory, monkeypatch):
    from fastapi import HTTPException

    import engram.config as config_mod

    plaintext, _ = await _seed_new_api_key(patch_auth_factory, scopes=["admin"])
    reset_principal_cache()
    # First auth caches the principal (TTL long enough to survive).
    monkeypatch.setattr(config_mod.settings, "api_key_cache_ttl_seconds", 3600)
    ok = await _resolve_direct(plaintext)
    assert ok is not None

    # Revoke the key.
    async with patch_auth_factory() as session:
        await session.execute(
            text("UPDATE api_keys SET revoked_at = '2026-01-02' WHERE label = 'test-new'")
        )
        await session.commit()

    # While cached (within TTL), the revoked key may still authenticate.
    _ = await _resolve_direct(plaintext)

    # After the cache entry expires (TTL=0 disables caching -> forces a DB lookup),
    # the revoked key is rejected.
    reset_principal_cache()
    monkeypatch.setattr(config_mod.settings, "api_key_cache_ttl_seconds", 0)
    with pytest.raises(HTTPException) as exc:
        await _resolve_direct(plaintext)
    assert exc.value.status_code == 401
    monkeypatch.setattr(config_mod.settings, "api_key_cache_ttl_seconds", 60)


# === Admin CRUD round-trip ===


async def test_create_workspace_and_api_key(make_client):
    c = make_client(auth_enabled=False)
    try:
        t = await c.post(
            "/v1/admin/tenants", json={"name": "Org", "slug": "org"}
        )
        assert t.status_code == 201
        tenant_id = t.json()["id"]

        w = await c.post(
            "/v1/admin/workspaces",
            json={"tenant_id": tenant_id, "name": "Eng", "slug": "eng"},
        )
        assert w.status_code == 201
        assert w.json()["slug"] == "eng"

        p = await c.post(
            "/v1/admin/principals",
            json={"tenant_id": tenant_id, "name": "ci-bot", "type": "agent"},
        )
        assert p.status_code == 201

        # API-key creation uses ARRAY(String) which requires Postgres.
        # The key generation + hashing logic is unit-tested above;
        # here we verify tenant/workspace/principal ORM CRUD works end-to-end.
    finally:
        await c.aclose()


async def test_duplicate_tenant_slug_conflict(make_client):
    c = make_client(auth_enabled=False)
    try:
        r1 = await c.post(
            "/v1/admin/tenants", json={"name": "Dup", "slug": "dupslug"}
        )
        assert r1.status_code == 201
        r2 = await c.post(
            "/v1/admin/tenants", json={"name": "Dup2", "slug": "dupslug"}
        )
        assert r2.status_code == 409
    finally:
        await c.aclose()


# === Workspace membership ===


async def test_check_workspace_membership_true(seeded):
    async with seeded() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
                "VALUES ('ws-1', '00000000-0000-0000-0000-000000000001', 'W', 'w', '2026-01-01')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO workspace_members (id, workspace_id, principal_id, role, created_at) "
                "VALUES ('wm-1', 'ws-1', 'p-admin', 'owner', '2026-01-01')"
            )
        )
        await session.commit()

    async with seeded() as session:
        is_member = await check_workspace_membership(
            session, principal_id="p-admin", workspace_id="ws-1"
        )
    assert is_member is True


async def test_check_workspace_membership_false(seeded):
    async with seeded() as session:
        await session.execute(
            text(
                "INSERT INTO workspaces (id, tenant_id, name, slug, created_at) "
                "VALUES ('ws-2', '00000000-0000-0000-0000-000000000001', 'W2', 'w2', '2026-01-01')"
            )
        )
        await session.commit()
    async with seeded() as session:
        is_member = await check_workspace_membership(
            session, principal_id="p-admin", workspace_id="ws-2"
        )
    assert is_member is False
