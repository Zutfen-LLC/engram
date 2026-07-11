"""Route-completeness enforcement and OpenAPI scope-policy generation (V2-BL-004).

Walks the live FastAPI application to find each route's declared
:class:`~engram.auth.ScopePolicy` (via its ``ScopeGuard``/``ExemptScopeGuard``
dependency) and builds a single ``{(method, path): ScopePolicy}`` map. That
map is the one source of truth for both:

* :func:`validate_scope_policy_completeness` — fails loudly (at app-startup
  time, and in tests) if any route has no policy, or more than one; and
* :func:`build_custom_openapi` — attaches the ``x-engram-scope-policy``
  vendor extension to every operation from the same map.

FastAPI 0.139 wraps ``include_router()``'d routes in an internal
``_IncludedRouter``/``RouteContext`` layer, so ``app.routes`` does not flatten
directly to ``APIRoute`` objects. ``fastapi.routing.iter_route_contexts`` is
the public entry point that yields one ``RouteContext`` per effective route
(proxying ``.path``/``.methods``/``.dependant`` to the merged view); framework
routes (``/openapi.json``, ``/docs``, ``/redoc``, the oauth2 redirect) are
plain Starlette ``Route`` objects, not ``APIRoute``, so filtering on
``isinstance(ctx.original_route, APIRoute)`` excludes them for free.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, iter_route_contexts

from engram.auth import ScopePolicy

RouteKey = tuple[str, str]  # (method, path)

_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def _iter_api_route_contexts(app: FastAPI) -> Iterator[Any]:
    for ctx in iter_route_contexts(app.routes):
        if isinstance(ctx.original_route, APIRoute):
            yield ctx


def validate_scope_policy_completeness(app: FastAPI) -> dict[RouteKey, ScopePolicy]:
    """Return the runtime ``{(method, path): ScopePolicy}`` map, or raise.

    Raises ``RuntimeError`` if any application route has zero scope-policy
    dependencies (undeclared — the scope invariant is violated) or more than
    one (conflicting guards on the same route).
    """
    result: dict[RouteKey, ScopePolicy] = {}
    for ctx in _iter_api_route_contexts(app):
        policies = [
            dep.call.policy
            for dep in ctx.dependant.dependencies
            if isinstance(getattr(dep.call, "policy", None), ScopePolicy)
        ]
        methods = sorted((ctx.methods or set()) - _IGNORED_METHODS)
        for method in methods:
            if not policies:
                raise RuntimeError(
                    f"{method} {ctx.path} has no scope policy declared "
                    "(add a ScopeGuard/ExemptScopeGuard dependency)"
                )
            if len(policies) > 1:
                raise RuntimeError(
                    f"{method} {ctx.path} has conflicting scope guards "
                    f"({len(policies)} declared — exactly one is required)"
                )
            result[(method, ctx.path)] = policies[0]
    return result


def policy_to_openapi_extension(policy: ScopePolicy) -> dict[str, Any]:
    """Render a :class:`ScopePolicy` as the ``x-engram-scope-policy`` value."""
    if policy.exempt:
        return {"exempt": True, "reason": policy.description or "exempt"}
    ext: dict[str, Any] = {}
    if policy.all_of:
        ext["all_of"] = list(policy.all_of)
    if policy.any_of:
        ext["any_of"] = list(policy.any_of)
    ext["admin_satisfies"] = True
    if policy.conditional:
        ext["conditional"] = dict(policy.conditional)
    return ext


def build_custom_openapi(app: FastAPI) -> dict[str, Any]:
    """FastAPI ``app.openapi()`` replacement — adds ``x-engram-scope-policy``.

    Re-validates completeness on every (uncached) build so a route added
    without a policy fails here too, not just at ``create_app()`` time.
    """
    if app.openapi_schema:
        return app.openapi_schema

    policies = validate_scope_policy_completeness(app)
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        summary=app.summary,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
    )
    for (method, path), policy in policies.items():
        path_item = schema.get("paths", {}).get(path)
        if not path_item:
            continue
        operation = path_item.get(method.lower())
        if operation is None:
            continue
        operation["x-engram-scope-policy"] = policy_to_openapi_extension(policy)

    app.openapi_schema = schema
    return schema
