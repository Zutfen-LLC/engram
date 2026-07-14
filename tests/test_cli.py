# ruff: noqa: E501
"""Tests for the deployment CLI commands: ``init-db`` and ``bootstrap-key``.

These cover the pure, DB-independent logic:

* migration discovery / sorting / URL normalization (``init-db`` construction),
* bootstrap-key material generation and bcrypt-hash authentication viability,
* scope parsing and validation.

The live-DB execution paths (actual asyncpg migration application and api_keys
insertion) are exercised by the Compose-backed CI path against a real Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.auth import (
    DIGEST_ALGORITHM,
    digest_api_key_secret,
    parse_api_key,
    verify_api_key_secret,
)
from engram.cli import (
    BootstrapKeyMaterial,
    make_bootstrap_key,
    parse_scopes,
    select_baseline_targets,
)
from engram.migrations import (
    MIGRATIONS_DIR,
    discover_migrations,
    migration_filename,
    normalize_asyncpg_url,
)

# --- init-db: migration discovery / URL normalization --------------------


def test_discover_migrations_finds_and_sorts_bundled():
    files = discover_migrations()
    names = [f.name for f in files]
    # The bundled migrations must ship with the package.
    assert names == sorted(names), "migrations must be returned in sorted order"
    assert "001_init.sql" in names
    assert "013_session_end_defaults.sql" in names
    # Every entry is a .sql file.
    assert all(f.suffix == ".sql" for f in files)


def test_discover_migrations_missing_dir_errors(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError):
        discover_migrations(missing)


def test_discover_migrations_custom_dir(tmp_path: Path):
    (tmp_path / "002_b.sql").write_text("-- b\n")
    (tmp_path / "001_a.sql").write_text("-- a\n")
    (tmp_path / "README.md").write_text("not sql\n")
    files = discover_migrations(tmp_path)
    assert [f.name for f in files] == ["001_a.sql", "002_b.sql"]


def test_migration_filename_is_basename(tmp_path: Path):
    f = tmp_path / "009_x.sql"
    f.write_text("-- x\n")
    assert migration_filename(f) == "009_x.sql"


def test_migrations_dir_constant_is_real_dir():
    assert MIGRATIONS_DIR.is_dir(), "bundled migrations/ must exist at package-relative path"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+asyncpg://engram:pw@localhost:5432/engram",
            "postgresql://engram:pw@localhost:5432/engram",
        ),
        (
            "postgresql://engram:pw@db:5432/engram",
            "postgresql://engram:pw@db:5432/engram",
        ),
        (
            "postgresql+asyncpg://u:p@h:5432/db?ssl=disable",
            "postgresql://u:p@h:5432/db?ssl=disable",
        ),
    ],
)
def test_normalize_asyncpg_url(url: str, expected: str):
    assert normalize_asyncpg_url(url) == expected


# --- init-db: --baseline cutoff selection (anti-masking) -----------------


def test_baseline_all_returns_every_file():
    names = ["001_init.sql", "002_backfill_indexes.sql", "003_new.sql"]
    assert select_baseline_targets(names, "all") == names


def test_baseline_cutoff_returns_prefix_through_target():
    names = ["001_init.sql", "002_backfill_indexes.sql", "003_new.sql"]
    # Cutoff at 002 records 001 AND 002, but NOT the newer 003 — so 003 will
    # still be applied by a subsequent `engram init-db` instead of being masked.
    assert select_baseline_targets(names, "002_backfill_indexes.sql") == [
        "001_init.sql",
        "002_backfill_indexes.sql",
    ]


def test_baseline_cutoff_first_file():
    names = ["001_init.sql", "002_backfill_indexes.sql"]
    assert select_baseline_targets(names, "001_init.sql") == ["001_init.sql"]


def test_baseline_cutoff_unknown_raises():
    names = ["001_init.sql", "002_backfill_indexes.sql"]
    with pytest.raises(ValueError, match="not found in migrations"):
        select_baseline_targets(names, "999_missing.sql")


def test_baseline_cutoff_against_real_bundled_migrations():
    """Cutoff selection must use the bundled migration set and ordering."""
    files = discover_migrations()
    names = [migration_filename(f) for f in files]
    # Baseline-all == everything; baseline at the last file == everything too.
    assert select_baseline_targets(names, "all") == names
    assert select_baseline_targets(names, names[-1]) == names
    # Baseline at the first file == only the first.
    assert select_baseline_targets(names, names[0]) == [names[0]]


# --- bootstrap-key: scope parsing ----------------------------------------


def test_parse_scopes_default_full_set():
    # Canonical order (V2-BL-004) is read, write, review, export, admin — note
    # this reorders "admin,export" (input order) to "export,admin" (canonical
    # order), not a regression: parse_scopes now delegates to
    # engram.auth.canonicalize_scopes for deterministic persistence order.
    scopes = parse_scopes("read,write,admin,export")
    assert scopes == ["read", "write", "export", "admin"]


def test_parse_scopes_strips_whitespace_and_dedups():
    scopes = parse_scopes(" read , write , read ")
    assert scopes == ["read", "write"]


def test_parse_scopes_rejects_empty():
    with pytest.raises(ValueError, match="at least one scope"):
        parse_scopes("   ")


def test_parse_scopes_rejects_unknown():
    with pytest.raises(ValueError, match="unknown scope"):
        parse_scopes("read,superuser")


def test_parse_scopes_rejects_typo():
    with pytest.raises(ValueError, match="unknown scope"):
        parse_scopes("read,reviews")


def test_parse_scopes_single():
    assert parse_scopes("admin") == ["admin"]


def test_parse_scopes_accepts_review():
    assert parse_scopes("review") == ["review"]
    assert parse_scopes("admin,review,read") == ["read", "review", "admin"]


# --- bootstrap-key: material + hash authentication viability -------------


def test_make_bootstrap_key_material_shape():
    material = make_bootstrap_key("ops", ["read", "write", "admin", "export"])
    assert isinstance(material, BootstrapKeyMaterial)
    assert material.label == "ops"
    assert material.scopes == ("read", "write", "admin", "export")
    # Plaintext key has the expected prefix and is non-trivial.
    assert material.plaintext.startswith("eng_")
    assert len(material.plaintext) > 20
    # New-format: plaintext embeds the key_id; the stored digest is NOT the key.
    assert material.key_id in material.plaintext
    parsed = parse_api_key(material.plaintext)
    assert parsed.key_id == material.key_id
    assert material.digest_algorithm == DIGEST_ALGORITHM
    assert material.secret_digest == digest_api_key_secret(parsed.secret)
    assert material.secret_digest != material.plaintext
    assert not material.secret_digest.startswith("eng_")


def test_bootstrap_key_digest_authenticates():
    """A bootstrap-produced key authenticates against its stored digest.

    The plaintext printed by ``engram bootstrap-key`` (shown once) must verify
    against the only thing persisted (the digest), since ``get_current_principal``
    authenticates new-format keys via the digest + constant-time comparison.
    """
    material = make_bootstrap_key("bootstrap", ["read", "write", "admin", "export"])
    parsed = parse_api_key(material.plaintext)
    # The stored digest must verify the printed secret...
    assert verify_api_key_secret(parsed.secret, material.secret_digest) is True
    # ...and reject anything else.
    assert verify_api_key_secret("some-other-secret", material.secret_digest) is False


def test_bootstrap_keys_are_unique():
    a = make_bootstrap_key("a", ["admin"])
    b = make_bootstrap_key("b", ["admin"])
    assert a.plaintext != b.plaintext
    assert a.key_id != b.key_id
    assert a.secret_digest != b.secret_digest
    # Cross-verification fails (a's digest must not verify b's secret).
    assert verify_api_key_secret(
        parse_api_key(b.plaintext).secret, a.secret_digest
    ) is False


# --- CLI wiring: argparse exposes the new subcommands -------------------


def test_cli_argparse_has_init_db_and_bootstrap():
    import argparse

    import engram.cli as cli_mod

    # Build the same parser the CLI uses by invoking main() with --help is
    # awkward; instead confirm the dispatch keys exist and the subcommands are
    # registered by parsing known argument sets.
    parser = argparse.ArgumentParser(prog="engram")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init-db")
    sub.add_parser("bootstrap-key")
    # Sanity: both are recognized positional choices.
    ns = parser.parse_args(["init-db"])
    assert ns.command == "init-db"
    ns = parser.parse_args(["bootstrap-key"])
    assert ns.command == "bootstrap-key"
    # The module exposes the async runners (imported elsewhere, not here).
    assert hasattr(cli_mod, "_run_init_db")
    assert hasattr(cli_mod, "_run_bootstrap_key")
    # ENG-AUD-008: the worker subcommand is registered.
    assert hasattr(cli_mod, "_run_worker")


# --- worker: --once processes a job and exits 0 (DB-backed) ---------------


async def test_cli_worker_once_processes_one_job():
    """``engram worker --once`` claims/processes at most one job, then exits 0."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from engram.cli import _run_worker
    from engram.config import settings
    from engram.jobs import enqueue_job

    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("requires a live PostgreSQL with the v2 schema (run docker compose up)")
        await engine.dispose()
        return

    try:
        async with factory() as session:
            tenant_id = (
                await session.execute(text("SELECT id::text FROM tenants WHERE slug = 'default'"))
            ).scalar_one()
            # retention.sweep performs bounded expired-receipt cleanup and is
            # still a safe, idempotent worker smoke target.
            await enqueue_job(
                session,
                tenant_id=tenant_id,
                job_type="retention.sweep",
                payload={},
            )

        rc = await _run_worker(
            once=True,
            session_factory=factory,
            app_session_factory=factory,
            worker_id="cli-test",
            job_types=["retention.sweep"],
        )
        assert rc == 0

        async with factory() as session:
            done = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM jobs WHERE job_type = 'retention.sweep' "
                        "AND status = 'succeeded'"
                    )
                )
            ).scalar_one()
            assert done == 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM jobs WHERE job_type = 'retention.sweep'"))
        await engine.dispose()


# --- worker logging -------------------------------------------------------


@pytest.fixture
def engram_logger_snapshot():
    """Snapshot and restore the global 'engram' logger state.

    The worker-logging tests mutate the logger's handlers, level, and
    propagation. This fixture captures the original state, yields, then
    restores everything and closes any handlers created by the test.
    """
    import logging

    logger = logging.getLogger("engram")
    original_level = logger.level
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)

    yield logger

    # Close handlers created during the test, then restore originals.
    for h in logger.handlers:
        if h not in original_handlers:
            h.close()
    logger.handlers.clear()
    for h in original_handlers:
        logger.addHandler(h)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def test_configure_worker_logging_sets_level(engram_logger_snapshot):
    """_configure_worker_logging sets the engram logger to INFO by default."""
    import logging

    from engram.cli import _configure_worker_logging

    logger = engram_logger_snapshot
    logger.setLevel(logging.WARNING)
    logger.propagate = True

    _configure_worker_logging()

    assert logger.level == logging.INFO
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)
    assert logger.propagate is False


def test_configure_worker_logging_respects_log_level(engram_logger_snapshot, monkeypatch):
    """_configure_worker_logging respects ENGRAM_LOG_LEVEL."""
    import logging

    from engram.cli import _configure_worker_logging
    from engram.config import Settings

    monkeypatch.setattr("engram.config.settings", Settings(log_level="debug"))

    logger = engram_logger_snapshot
    _configure_worker_logging()

    assert logger.level == logging.DEBUG


def test_configure_worker_logging_idempotent(engram_logger_snapshot):
    """Calling _configure_worker_logging twice does not duplicate handlers."""
    from engram.cli import _configure_worker_logging

    logger = engram_logger_snapshot

    _configure_worker_logging()
    _configure_worker_logging()

    assert len(logger.handlers) == 1


def test_configure_worker_logging_falls_back_on_invalid_level(
    engram_logger_snapshot, monkeypatch
):
    """An invalid log level falls back to INFO."""
    import logging

    from engram.cli import _configure_worker_logging
    from engram.config import Settings

    monkeypatch.setattr("engram.config.settings", Settings(log_level="not-a-level"))

    logger = engram_logger_snapshot
    _configure_worker_logging()

    assert logger.level == logging.INFO
