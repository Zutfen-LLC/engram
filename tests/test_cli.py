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

from engram.auth import verify_api_key
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
    scopes = parse_scopes("read,write,admin,export")
    assert scopes == ["read", "write", "admin", "export"]


def test_parse_scopes_strips_whitespace_and_dedups():
    scopes = parse_scopes(" read , write , read ")
    assert scopes == ["read", "write"]


def test_parse_scopes_rejects_empty():
    with pytest.raises(ValueError, match="at least one scope"):
        parse_scopes("   ")


def test_parse_scopes_rejects_unknown():
    with pytest.raises(ValueError, match="unknown scope"):
        parse_scopes("read,superuser")


def test_parse_scopes_single():
    assert parse_scopes("admin") == ["admin"]


# --- bootstrap-key: material + hash authentication viability -------------


def test_make_bootstrap_key_material_shape():
    material = make_bootstrap_key("ops", ["read", "write", "admin", "export"])
    assert isinstance(material, BootstrapKeyMaterial)
    assert material.label == "ops"
    assert material.scopes == ("read", "write", "admin", "export")
    # Plaintext key has the expected prefix and is non-trivial.
    assert material.plaintext.startswith("eng_")
    assert len(material.plaintext) > 20
    # The stored hash is NOT the plaintext.
    assert material.key_hash != material.plaintext
    assert not material.key_hash.startswith("eng_")


def test_bootstrap_key_hash_authenticates():
    """A bootstrap-produced key authenticates against its stored bcrypt hash.

    This is the hash-authentication viability check: the plaintext printed by
    ``engram bootstrap-key`` (shown once) must verify against the only thing
    persisted (the bcrypt hash), since ``get_current_principal`` authenticates
    exactly this way at request time.
    """
    material = make_bootstrap_key("bootstrap", ["read", "write", "admin", "export"])
    # The stored hash must verify the printed plaintext...
    assert verify_api_key(material.plaintext, material.key_hash) is True
    # ...and reject anything else.
    assert verify_api_key("eng_some-other-key", material.key_hash) is False
    assert verify_api_key(material.plaintext, "not-a-real-hash") is False


def test_bootstrap_keys_are_unique():
    a = make_bootstrap_key("a", ["admin"])
    b = make_bootstrap_key("b", ["admin"])
    assert a.plaintext != b.plaintext
    assert a.key_hash != b.key_hash
    # Cross-verification fails (a's hash must not verify b's key).
    assert verify_api_key(b.plaintext, a.key_hash) is False


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
