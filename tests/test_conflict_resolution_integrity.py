"""Narrow structural guards for governed, serialized conflict resolution.

The app-role behavioral suites remain authoritative.  These guards make the
load-bearing transaction and policy calls explicit when PostgreSQL is not
available to a fast local test run.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from engram.api.routes.review import resolve_conflict


def _source() -> str:
    return inspect.getsource(resolve_conflict)


def _call_names() -> set[str]:
    tree = ast.parse(textwrap.dedent(_source()))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_route_keeps_principal_type_and_calls_conflict_policy() -> None:
    source = _source()
    assert "principal_id, principal_type = await _resolve_principal" in source
    assert "can_resolve_conflict" in _call_names()


def test_pair_query_orders_rows_before_locking() -> None:
    source = _source()
    assert "pair_ids = sorted" in source
    assert source.index(".order_by(MemoryItem.id)") < source.index(".with_for_update()")


def test_guarded_update_requires_unresolved_status_and_original_link() -> None:
    source = _source()
    update_start = source.index("update(MemoryItem)")
    update_end = source.index(".returning(MemoryItem.id)", update_start)
    guarded_update = source[update_start:update_end]
    assert 'MemoryItem.conflict_resolution_status == "unresolved"' in guarded_update
    assert "MemoryItem.conflicts_with_item_id == candidate_counterpart_id" in guarded_update
    assert "conflict_resolved_by=actor" in guarded_update


def test_same_resolution_returns_before_event_write() -> None:
    source = _source()
    unchanged_return = source.index('status="unchanged"')
    event_write = source.index("event = await _insert_item_event")
    assert unchanged_return < event_write


def test_event_and_update_share_the_single_commit() -> None:
    source = _source()
    assert source.count("await session.commit()") == 1
    assert source.index("event = await _insert_item_event") < source.index(
        "await session.commit()"
    )
