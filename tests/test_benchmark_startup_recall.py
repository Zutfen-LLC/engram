from __future__ import annotations

import inspect
import uuid
from typing import Any

from engram.auth import Principal
from engram.memory_context import unrestricted_memory_context
from engram.recall import _fetch_active_items, execute_startup_recall
from scripts import benchmark_startup_recall


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _SessionFactory:
    def __call__(self) -> _SessionContext:
        return _SessionContext()


async def test_benchmark_reaches_both_recall_paths_with_one_resolved_context(
    monkeypatch,
) -> None:
    tenant_id = str(uuid.uuid4())
    principal_id = str(uuid.uuid4())
    memory_context = unrestricted_memory_context(
        Principal(tenant_id=tenant_id, principal_id=principal_id, scopes=("read",))
    )
    fetch_signature = inspect.signature(_fetch_active_items)
    execute_signature = inspect.signature(execute_startup_recall)
    seen: list[tuple[str, object]] = []

    async def apply_rls(session: object, *, tenant_id: str, principal_id: str) -> None:
        assert session is not None
        assert tenant_id == str(memory_context.tenant_id)
        assert principal_id == str(memory_context.principal_id)

    async def fetch(session: object, passed_context: object, workspace_id: object) -> list[object]:
        fetch_signature.bind(session, passed_context, workspace_id)
        seen.append(("fetch", passed_context))
        return []

    async def execute(**kwargs: Any) -> dict[str, Any]:
        execute_signature.bind(**kwargs)
        seen.append(("execute", kwargs["memory_context"]))
        return {}

    monkeypatch.setattr(benchmark_startup_recall, "apply_rls_context", apply_rls)
    monkeypatch.setattr(benchmark_startup_recall, "_fetch_active_items", fetch)
    monkeypatch.setattr(benchmark_startup_recall, "execute_startup_recall", execute)

    await benchmark_startup_recall._measure_recall_paths(  # type: ignore[arg-type]
        _SessionFactory(), memory_context
    )

    assert seen == [("fetch", memory_context), ("execute", memory_context)]
