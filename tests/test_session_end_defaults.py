from __future__ import annotations

from types import SimpleNamespace
from typing import get_args
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from engram.api.routes.memory import (
    _SOURCE_TRUST_KEYS,
    _TRUST_FALLBACKS,
    SourceKind,
    _resolve_trust_defaults,
)
from engram.models import TenantConfig

SOURCES = set(get_args(SourceKind))


def _config() -> SimpleNamespace:
    values = {
        f"{prefix}_{key}": value[index]
        for key, value in _TRUST_FALLBACKS.items()
        for index, prefix in enumerate(("trust", "confidence"))
    }
    return SimpleNamespace(**values)


@pytest.mark.parametrize("source_type", sorted(SOURCES))
@pytest.mark.parametrize("has_config", [False, True])
async def test_every_source_resolves_complete_bounded_defaults(source_type: str, has_config: bool):
    result = SimpleNamespace(scalar_one_or_none=lambda: _config() if has_config else None)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    trust, confidence, review = await _resolve_trust_defaults(
        session, uuid4(), source_type, "admin"
    )
    assert 0.0 <= trust <= 1.0
    assert 0.0 <= confidence <= 1.0
    assert review in {"active", "proposed"}


def test_source_vocabulary_default_and_orm_mappings_cannot_drift() -> None:
    assert set(_SOURCE_TRUST_KEYS) == SOURCES
    keys = {key for pair in _SOURCE_TRUST_KEYS.values() for key in pair}
    assert keys <= set(_TRUST_FALLBACKS)
    assert all(hasattr(TenantConfig, f"trust_{key}") for key in keys)
    assert all(hasattr(TenantConfig, f"confidence_{key}") for key in keys)


def test_unknown_source_type_remains_rejected() -> None:
    from engram.api.routes.memory import RememberRequest

    with pytest.raises(ValidationError):
        RememberRequest.model_validate({"content": "invalid", "source_type": "unknown"})
