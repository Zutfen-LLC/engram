"""Select deterministic, size-balanced root-test shards."""

from __future__ import annotations

import argparse
from pathlib import Path


def build_root_test_shards(
    test_root: Path, *, shard_count: int
) -> tuple[tuple[Path, ...], ...]:
    """Assign each root test file to one size-balanced shard."""
    if shard_count < 1:
        raise ValueError("shard count must be at least 1")

    test_files = sorted(
        {*test_root.rglob("test_*.py"), *test_root.rglob("*_test.py")},
        key=lambda path: (-path.stat().st_size, path.as_posix()),
    )
    if not test_files:
        raise ValueError(f"no root test files found under {test_root}")

    shards: list[list[Path]] = [[] for _ in range(shard_count)]
    shard_sizes = [0] * shard_count
    for path in test_files:
        shard_index = min(range(shard_count), key=lambda index: (shard_sizes[index], index))
        shards[shard_index].append(path)
        shard_sizes[shard_index] += path.stat().st_size

    return tuple(tuple(sorted(shard)) for shard in shards)


def select_root_test_shard(
    test_root: Path, *, shard_index: int, shard_count: int
) -> tuple[Path, ...]:
    """Return one zero-indexed shard from the complete root test suite."""
    if not 0 <= shard_index < shard_count:
        raise ValueError(
            f"shard index must be between 0 and {shard_count - 1}; got {shard_index}"
        )
    return build_root_test_shards(test_root, shard_count=shard_count)[shard_index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--test-root", type=Path, default=Path("tests"))
    args = parser.parse_args()

    for path in select_root_test_shard(
        args.test_root,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
    ):
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
