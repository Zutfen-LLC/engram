"""Tests for deterministic root-suite sharding."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci_shards import build_root_test_shards, select_root_test_shard

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPOSITORY_ROOT / "tests"


def test_root_test_shards_are_complete_disjoint_and_size_balanced() -> None:
    shards = build_root_test_shards(TESTS_ROOT, shard_count=4)
    expected = {*TESTS_ROOT.rglob("test_*.py"), *TESTS_ROOT.rglob("*_test.py")}
    selected = [path for shard in shards for path in shard]

    assert len(shards) == 4
    assert all(shards)
    assert len(selected) == len(set(selected))
    assert set(selected) == expected

    shard_sizes = [sum(path.stat().st_size for path in shard) for shard in shards]
    largest_file = max(path.stat().st_size for path in expected)
    assert max(shard_sizes) - min(shard_sizes) <= largest_file


def test_root_test_shards_are_deterministic() -> None:
    first = build_root_test_shards(TESTS_ROOT, shard_count=4)
    second = build_root_test_shards(TESTS_ROOT, shard_count=4)

    assert first == second


def test_root_test_shards_support_both_pytest_filename_patterns(tmp_path: Path) -> None:
    test_prefix = tmp_path / "test_prefix.py"
    test_suffix = tmp_path / "suffix_test.py"
    ignored = tmp_path / "helper.py"
    for path in (test_prefix, test_suffix, ignored):
        path.write_text("pass\n")

    shards = build_root_test_shards(tmp_path, shard_count=1)

    assert len(shards) == 1
    assert set(shards[0]) == {test_prefix, test_suffix}


@pytest.mark.parametrize("shard_index", [-1, 4])
def test_select_root_test_shard_rejects_an_invalid_index(shard_index: int) -> None:
    with pytest.raises(ValueError, match="shard index"):
        select_root_test_shard(TESTS_ROOT, shard_index=shard_index, shard_count=4)


def test_build_root_test_shards_rejects_an_invalid_count() -> None:
    with pytest.raises(ValueError, match="shard count"):
        build_root_test_shards(TESTS_ROOT, shard_count=0)
