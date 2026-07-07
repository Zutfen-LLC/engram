"""Tests for content canonicalization and hashing."""

from __future__ import annotations

import re

from engram.canonicalize import canonicalize, content_hash


class TestCanonicalize:
    def test_strips_and_collapses_and_lowercases(self) -> None:
        assert canonicalize("  Hello   World  ") == "hello world"

    def test_empty_string(self) -> None:
        assert canonicalize("") == ""

    def test_whitespace_only(self) -> None:
        assert canonicalize("   \t\n  ") == ""

    def test_preserves_internal_single_spaces(self) -> None:
        assert canonicalize("hello world") == "hello world"

    def test_collapses_tabs_and_newlines(self) -> None:
        assert canonicalize("Hello\tWorld\nFoo") == "hello world foo"


class TestContentHash:
    def test_format_is_sha256_prefix_plus_64_hex(self) -> None:
        h = content_hash(canonicalize("Hello World"))
        assert re.match(r"^sha256:[0-9a-f]{64}$", h) is not None

    def test_identical_strings_same_hash(self) -> None:
        h1 = content_hash(canonicalize("Hello World"))
        h2 = content_hash(canonicalize("  hello   world  "))
        assert h1 == h2

    def test_different_strings_different_hashes(self) -> None:
        h1 = content_hash(canonicalize("Hello World"))
        h2 = content_hash(canonicalize("Goodbye World"))
        assert h1 != h2

    def test_empty_string_handled_gracefully(self) -> None:
        h = content_hash(canonicalize(""))
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_deterministic(self) -> None:
        h1 = content_hash(canonicalize("test content"))
        h2 = content_hash(canonicalize("test content"))
        assert h1 == h2
