"""Pure unit tests for the V2-BL-004 scope-evaluation primitives.

No database, no FastAPI app — these exercise `engram.auth`'s scope
vocabulary, `Principal.has_scope`/`principal_has_scope`, `ScopeGuard`/
`ExemptScopeGuard`, and `canonicalize_scopes` in isolation.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from engram.auth import (
    ADMIN_SCOPE,
    CANONICAL_SCOPE_ORDER,
    EXPORT_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    VALID_SCOPES,
    WRITE_OR_REVIEW_SCOPE,
    WRITE_SCOPE,
    Principal,
    ScopeGuard,
    ScopePolicy,
    canonicalize_scopes,
    principal_has_scope,
    require_any_scope,
    require_scopes,
)


def _principal(*scopes: str) -> Principal:
    return Principal(tenant_id="t1", principal_id="p1", scopes=tuple(scopes))


# --- Scope vocabulary --------------------------------------------------------


def test_valid_scopes_is_exactly_five():
    assert {"read", "write", "review", "export", "admin"} == VALID_SCOPES


def test_canonical_scope_order():
    assert CANONICAL_SCOPE_ORDER == ("read", "write", "review", "export", "admin")


# --- principal_has_scope / Principal.has_scope -------------------------------


def test_exact_scope_match_succeeds():
    assert principal_has_scope(_principal("read"), "read") is True


def test_missing_scope_fails():
    assert principal_has_scope(_principal("read"), "write") is False


@pytest.mark.parametrize("required", ["read", "write", "review", "export", "admin"])
def test_admin_satisfies_every_scope(required):
    assert principal_has_scope(_principal("admin"), required) is True


def test_read_does_not_satisfy_write():
    assert principal_has_scope(_principal("read"), "write") is False


def test_write_does_not_satisfy_read():
    assert principal_has_scope(_principal("write"), "read") is False


def test_review_does_not_satisfy_read():
    assert principal_has_scope(_principal("review"), "read") is False


def test_review_does_not_satisfy_write():
    assert principal_has_scope(_principal("review"), "write") is False


def test_export_does_not_satisfy_read():
    assert principal_has_scope(_principal("export"), "read") is False


def test_unknown_scope_strings_confer_nothing():
    p = _principal("unknown_legacy_scope", "read")
    assert principal_has_scope(p, "read") is True
    assert principal_has_scope(p, "write") is False
    # The unknown string itself is never a valid "required" value in practice,
    # but even checked directly it isn't treated as a wildcard/admin-like grant.
    assert principal_has_scope(p, "admin") is False


def test_empty_granted_scope_set():
    p = _principal()
    for scope in VALID_SCOPES:
        assert principal_has_scope(p, scope) is False


def test_has_scope_method_matches_module_function():
    p = _principal("write")
    assert p.has_scope("write") == principal_has_scope(p, "write")
    assert p.has_scope("read") == principal_has_scope(p, "read")


# --- canonicalize_scopes ------------------------------------------------------


def test_canonicalize_dedupes_and_orders():
    assert canonicalize_scopes(["review", "read", "review"]) == ["read", "review"]


def test_canonicalize_full_vocabulary_order():
    assert canonicalize_scopes(["admin", "export", "review", "write", "read"]) == [
        "read",
        "write",
        "review",
        "export",
        "admin",
    ]


def test_canonicalize_empty_stays_empty():
    assert canonicalize_scopes([]) == []


def test_canonicalize_rejects_unknown():
    with pytest.raises(ValueError, match="unknown scope"):
        canonicalize_scopes(["read", "reviews"])


def test_canonicalize_does_not_silently_correct_typo():
    # A typo must not be coerced to the nearest valid scope.
    with pytest.raises(ValueError):
        canonicalize_scopes(["admni"])


# --- ScopeGuard: all_of / any_of / exempt ------------------------------------


@pytest.mark.asyncio
async def test_scope_guard_all_of_passes_when_scope_present():
    principal = await READ_SCOPE(_principal("read"))
    assert principal.scopes == ("read",)


@pytest.mark.asyncio
async def test_scope_guard_all_of_raises_403_when_missing():
    with pytest.raises(HTTPException) as exc_info:
        await READ_SCOPE(_principal("write"))
    assert exc_info.value.status_code == 403
    assert "read" in exc_info.value.detail


@pytest.mark.asyncio
async def test_scope_guard_all_of_admin_satisfies():
    await WRITE_SCOPE(_principal("admin"))
    await REVIEW_SCOPE(_principal("admin"))
    await EXPORT_SCOPE(_principal("admin"))
    await ADMIN_SCOPE(_principal("admin"))


@pytest.mark.asyncio
async def test_scope_guard_any_of_passes_with_either_scope():
    await WRITE_OR_REVIEW_SCOPE(_principal("write"))
    await WRITE_OR_REVIEW_SCOPE(_principal("review"))
    await WRITE_OR_REVIEW_SCOPE(_principal("admin"))


@pytest.mark.asyncio
async def test_scope_guard_any_of_raises_403_when_neither_present():
    with pytest.raises(HTTPException) as exc_info:
        await WRITE_OR_REVIEW_SCOPE(_principal("read"))
    assert exc_info.value.status_code == 403
    assert "one of scopes" in exc_info.value.detail


@pytest.mark.asyncio
async def test_scope_guard_multiple_missing_scopes_message_is_plural():
    guard = ScopeGuard(all_of=("write", "review"))
    with pytest.raises(HTTPException) as exc_info:
        await guard(_principal())
    assert "scopes" in exc_info.value.detail  # plural form
    assert "write" in exc_info.value.detail and "review" in exc_info.value.detail


def test_scope_guard_rejects_unknown_scope_at_construction():
    with pytest.raises(ValueError):
        ScopeGuard(all_of=("bogus",))


def test_scope_guard_requires_all_of_or_any_of():
    with pytest.raises(ValueError):
        ScopeGuard()


@pytest.mark.asyncio
async def test_exempt_guard_is_a_noop():
    from engram.auth import ExemptScopeGuard

    guard = ExemptScopeGuard(reason="test")
    assert guard.policy == ScopePolicy(exempt=True, description="test")
    assert await guard() is None


def test_require_scopes_returns_scope_guard_all_of():
    guard = require_scopes("read", "write")
    assert isinstance(guard, ScopeGuard)
    assert guard.policy.all_of == ("read", "write")


def test_require_any_scope_returns_scope_guard_any_of():
    guard = require_any_scope("write", "review")
    assert isinstance(guard, ScopeGuard)
    assert guard.policy.any_of == ("write", "review")
