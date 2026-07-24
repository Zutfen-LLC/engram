"""OpenAPI schema construction for the Control Plane.

Deterministic and database-free. The same app instance always yields the same
schema (sorted keys, stable field order), which the ``export-openapi`` CLI and
the contract drift check rely on.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from engram_control_plane import __version__


def build_openapi(app: FastAPI) -> dict[str, Any]:
    """Return a freshly built, deterministic OpenAPI schema for ``app``.

    Always rebuilt (never cached on the app) so ``export-openapi`` and the drift
    check see a stable, canonical document. The schema is the single source of
    truth for the portal-sdk generated types.
    """
    schema = get_openapi(
        title=app.title,
        version=__version__,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    return schema


def canonical_openapi_json(app: FastAPI) -> str:
    """Return the OpenAPI schema as canonical (sorted-keys, 2-space) JSON text.

    ``ensure_ascii=False`` keeps the document readable; ``sort_keys=True`` plus
    a fixed indent makes the output byte-stable across runs and machines.
    """
    schema = build_openapi(app)
    # Recurse to sort all nested keys so output is independent of dict ordering.
    return json.dumps(_sort_keys_deep(schema), indent=2, ensure_ascii=False) + "\n"


def _sort_keys_deep(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sort_keys_deep(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_sort_keys_deep(v) for v in value]
    return value
