"""Route-completeness enforcement for scope policies (V2-BL-004).

Walks the real application to prove every caller-facing route has exactly
one explicit scope policy (or is exempt), that the runtime policy map matches
the ticket's canonical route-to-scope matrix, and that the OpenAPI schema's
`x-engram-scope-policy` extension is derived from — and matches — that same
runtime map. Also proves the completeness check actually fails on a
synthetic unscoped/conflicting route, so a future route added without a
policy breaks CI rather than shipping unprotected.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRouter

from engram.api.app import app
from engram.api.scope_policy import (
    build_custom_openapi,
    policy_to_openapi_extension,
    validate_scope_policy_completeness,
)
from engram.auth import (
    ADMIN_SCOPE,
    EXPORT_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_OR_REVIEW_SCOPE,
    WRITE_SCOPE,
    ScopeGuard,
)

# The ticket's canonical route matrix. `all_of`/`any_of` are checked against
# the runtime ScopePolicy; a mismatch here means a route was reclassified
# (e.g. accidentally loosened or tightened) without updating the ticket.
EXPECTED_MATRIX: dict[tuple[str, str], dict] = {
    ("GET", "/health"): {"exempt": True},
    ("GET", "/ready"): {"exempt": True},
    ("GET", "/whoami"): {"all_of": ("read",)},
    ("POST", "/v1/remember"): {"all_of": ("write",)},
    ("POST", "/v1/recall"): {"all_of": ("read",)},
    ("POST", "/v1/search"): {"all_of": ("read",)},
    ("POST", "/v1/feedback"): {"all_of": ("write",)},
    ("GET", "/v1/items"): {"all_of": ("read",)},
    ("GET", "/v1/items/{item_id}"): {"all_of": ("read",)},
    ("PATCH", "/v1/items/{item_id}"): {"all_of": ("write",)},
    ("POST", "/v1/items/{item_id}/supersede"): {"all_of": ("write",)},
    ("POST", "/v1/items/{item_id}/invalidate"): {"all_of": ("write",)},
    ("GET", "/v1/review/queue"): {"all_of": ("review",)},
    ("GET", "/v1/review/conflicts"): {"all_of": ("review",)},
    ("GET", "/v1/review/stale"): {"all_of": ("review",)},
    ("GET", "/v1/review/stats"): {"all_of": ("review",)},
    ("POST", "/v1/items/{item_id}/review"): {"any_of": ("write", "review")},
    ("POST", "/v1/items/{item_id}/verify"): {"all_of": ("review",)},
    ("POST", "/v1/items/{item_id}/resolve-conflict"): {"all_of": ("review",)},
    ("POST", "/v1/items/bulk-archive"): {"all_of": ("review",)},
    ("POST", "/v1/kg"): {"all_of": ("write",)},
    ("GET", "/v1/kg/query"): {"all_of": ("read",)},
    ("POST", "/v1/kg/invalidate"): {"all_of": ("write",)},
    ("GET", "/v1/kg/timeline"): {"all_of": ("read",)},
    ("GET", "/v1/taxonomy"): {"all_of": ("read",)},
    ("GET", "/v1/tunnels"): {"all_of": ("read",)},
    ("POST", "/v1/tunnels"): {"all_of": ("write",)},
    ("POST", "/v1/diary"): {"all_of": ("write",)},
    ("GET", "/v1/diary/{principal}"): {"all_of": ("read",)},
    ("POST", "/v1/classify"): {"all_of": ("read",)},
    ("GET", "/v1/classification/rules"): {"all_of": ("admin",)},
    ("POST", "/v1/classification/rules"): {"all_of": ("admin",)},
    ("DELETE", "/v1/classification/rules/{rule_id}"): {"all_of": ("admin",)},
    ("POST", "/v1/agents"): {"all_of": ("write",)},
    ("GET", "/v1/agents"): {"all_of": ("read",)},
    ("DELETE", "/v1/agents/{agent_id}"): {"all_of": ("write",)},
    ("GET", "/v1/export/cca"): {"all_of": ("export",)},
    ("POST", "/v1/admin/tenants"): {"all_of": ("admin",)},
    ("POST", "/v1/admin/workspaces"): {"all_of": ("admin",)},
    ("POST", "/v1/admin/principals"): {"all_of": ("admin",)},
    ("GET", "/v1/admin/principals"): {"all_of": ("admin",)},
    ("POST", "/v1/admin/api-keys"): {"all_of": ("admin",)},
    ("GET", "/v1/admin/memory-kinds"): {"all_of": ("admin",)},
    ("POST", "/v1/admin/memory-kinds"): {"all_of": ("admin",)},
    ("PATCH", "/v1/admin/memory-kinds/{name}"): {"all_of": ("admin",)},
    ("POST", "/v1/admin/promote"): {"all_of": ("admin",)},
}


@pytest.fixture(scope="module")
def runtime_policy_map():
    return validate_scope_policy_completeness(app)


def test_runtime_matrix_matches_canonical_matrix(runtime_policy_map):
    assert set(runtime_policy_map) == set(EXPECTED_MATRIX), (
        "route inventory drifted from the canonical matrix — "
        f"missing: {set(EXPECTED_MATRIX) - set(runtime_policy_map)}, "
        f"unexpected: {set(runtime_policy_map) - set(EXPECTED_MATRIX)}"
    )
    for key, expected in EXPECTED_MATRIX.items():
        policy = runtime_policy_map[key]
        if expected.get("exempt"):
            assert policy.exempt, f"{key} should be exempt"
        else:
            assert not policy.exempt, f"{key} should not be exempt"
            assert policy.all_of == expected.get("all_of", ()), key
            assert policy.any_of == expected.get("any_of", ()), key


def test_only_health_and_ready_are_exempt(runtime_policy_map):
    exempt_routes = {k for k, p in runtime_policy_map.items() if p.exempt}
    assert exempt_routes == {("GET", "/health"), ("GET", "/ready")}


def test_every_admin_route_requires_admin_scope(runtime_policy_map):
    for (method, path), policy in runtime_policy_map.items():
        if path.startswith("/v1/admin/"):
            assert policy.all_of == ("admin",), f"{method} {path} must require admin"


def test_all_declared_scopes_are_valid(runtime_policy_map):
    from engram.auth import VALID_SCOPES

    for policy in runtime_policy_map.values():
        assert set(policy.all_of) <= VALID_SCOPES
        assert set(policy.any_of) <= VALID_SCOPES


def test_openapi_matches_runtime_policy(runtime_policy_map):
    schema = build_custom_openapi(app)
    for (method, path), policy in runtime_policy_map.items():
        operation = schema["paths"][path][method.lower()]
        assert "x-engram-scope-policy" in operation, f"missing extension for {method} {path}"
        assert operation["x-engram-scope-policy"] == policy_to_openapi_extension(policy)


def test_every_operation_has_the_extension():
    schema = build_custom_openapi(app)
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            assert "x-engram-scope-policy" in operation, f"{method.upper()} {path} has no policy"


# --- Negative cases: the check must actually fail on bad input --------------


def test_synthetic_unscoped_route_fails_completeness():
    synthetic = FastAPI()
    router = APIRouter()

    @router.get("/synthetic/unscoped")
    async def _unscoped():
        return {}

    synthetic.include_router(router)
    with pytest.raises(RuntimeError, match="no scope policy declared"):
        validate_scope_policy_completeness(synthetic)


def test_synthetic_conflicting_guards_fails_completeness():
    synthetic = FastAPI()
    router = APIRouter()

    @router.get(
        "/synthetic/conflict",
        dependencies=[Depends(READ_SCOPE), Depends(WRITE_SCOPE)],
    )
    async def _conflict():
        return {}

    synthetic.include_router(router)
    with pytest.raises(RuntimeError, match="conflicting scope guards"):
        validate_scope_policy_completeness(synthetic)


def test_unknown_scope_cannot_construct_a_guard():
    with pytest.raises(ValueError, match="Unknown scope"):
        ScopeGuard(all_of=("bogus_scope",))


def test_shared_constants_are_used_consistently():
    # Sanity check that the shared constants imported above are exactly what
    # the real routes use — if a route constructed its own ad hoc ScopeGuard
    # instead of importing these, this wouldn't catch it directly, but a
    # divergent value here would signal the constants themselves regressed.
    assert READ_SCOPE.policy.all_of == ("read",)
    assert WRITE_SCOPE.policy.all_of == ("write",)
    assert REVIEW_SCOPE.policy.all_of == ("review",)
    assert EXPORT_SCOPE.policy.all_of == ("export",)
    assert ADMIN_SCOPE.policy.all_of == ("admin",)
    assert WRITE_OR_REVIEW_SCOPE.policy.any_of == ("write", "review")
