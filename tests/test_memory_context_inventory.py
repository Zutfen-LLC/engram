from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi.routing import APIRoute

from engram.api.app import create_app
from engram.memory_context import resolve_memory_context

_MEMORY_ITEM_READ_ROUTES = {
    ("POST", "/v1/recall"),
    ("POST", "/v1/search"),
    ("GET", "/v1/items"),
    ("GET", "/v1/items/{item_id}"),
    ("GET", "/v1/kg/query"),
    ("GET", "/v1/kg/timeline"),
    ("GET", "/v1/review/queue"),
    ("GET", "/v1/review/conflicts"),
    ("GET", "/v1/review/stale"),
    ("GET", "/v1/review/stats"),
    ("GET", "/v1/diary/{principal}"),
    ("GET", "/v1/export/cca"),
    ("GET", "/v1/taxonomy"),
}

_PRE_002C_MUTATIONS = {
    ("POST", "/v1/remember"),
    ("POST", "/v1/classify"),
    ("POST", "/v1/kg"),
    ("POST", "/v1/kg/invalidate"),
    ("POST", "/v1/diary"),
    ("POST", "/v1/feedback"),
    ("PATCH", "/v1/items/{item_id}"),
    ("POST", "/v1/items/{item_id}/supersede"),
    ("POST", "/v1/items/{item_id}/invalidate"),
    ("POST", "/v1/items/{item_id}/review"),
    ("POST", "/v1/items/{item_id}/verify"),
    ("POST", "/v1/items/{item_id}/resolve-conflict"),
    ("POST", "/v1/items/bulk-archive"),
}

_EXPLICITLY_UNAFFECTED_GET_ROUTES = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("GET", "/whoami"),
    ("GET", "/v1/agents"),
    ("GET", "/v1/memory-profiles"),
    ("GET", "/v1/memory-profiles/{profile_id}"),
    ("GET", "/v1/memory-profiles/{profile_id}/revisions"),
    ("GET", "/v1/classification/rules"),
    ("GET", "/v1/tunnels"),
    ("GET", "/v1/admin/principals"),
    ("GET", "/v1/admin/memory-kinds"),
}


def _dependency_calls(route: APIRoute) -> Iterable[Any]:
    stack = list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        yield dependency.call
        stack.extend(dependency.dependencies)


def _routes() -> dict[tuple[str, str], APIRoute]:
    result: dict[tuple[str, str], APIRoute] = {}
    for included in create_app().routes:
        contexts = getattr(included, "effective_route_contexts", None)
        if contexts is None:
            continue
        for context in contexts():
            route = context.original_route
            if not isinstance(route, APIRoute):
                continue
            for method in context.methods:
                result[(method, context.path)] = route
    return result


def test_every_memory_item_read_surface_resolves_one_context() -> None:
    routes = _routes()
    assert routes.keys() >= _MEMORY_ITEM_READ_ROUTES
    # Every GET must remain explicitly classified. A future caller-facing
    # route therefore fails this inventory until its author chooses either
    # the MemoryItem context boundary or a reviewed control-plane exemption.
    get_routes = {key for key in routes if key[0] == "GET"}
    memory_get_routes = {key for key in _MEMORY_ITEM_READ_ROUTES if key[0] == "GET"}
    assert memory_get_routes.isdisjoint(_EXPLICITLY_UNAFFECTED_GET_ROUTES)
    assert get_routes == memory_get_routes | _EXPLICITLY_UNAFFECTED_GET_ROUTES
    for key in _MEMORY_ITEM_READ_ROUTES:
        calls = list(_dependency_calls(routes[key]))
        assert calls.count(resolve_memory_context) == 1, key


def test_pre_002c_mutations_do_not_resolve_profile_read_policy() -> None:
    routes = _routes()
    assert routes.keys() >= _PRE_002C_MUTATIONS
    for key in _PRE_002C_MUTATIONS:
        assert resolve_memory_context not in set(_dependency_calls(routes[key])), key


def test_no_data_plane_schema_accepts_a_profile_selector() -> None:
    schema = create_app().openapi()
    for path, operation in schema["paths"].items():
        if path.startswith("/v1/memory-profiles") or path.startswith("/v1/admin/api-keys"):
            continue
        rendered = str(operation).lower()
        assert "memory_profile_id" not in rendered, path
        assert "memory_profile_revision_id" not in rendered, path
