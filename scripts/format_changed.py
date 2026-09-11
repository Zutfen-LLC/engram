"""Run Ruff formatting only on Python files changed between two Git revisions."""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

PYTHON_SUFFIXES = {".py", ".pyi"}


def changed_python_files(base: str, head: str) -> tuple[Path, ...]:
    """Return existing Python paths changed from *base* to *head*."""
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACM",
            "-z",
            base,
            head,
            "--",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    paths: list[Path] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = Path(os.fsdecode(raw_path))
        if path.suffix in PYTHON_SUFFIXES and path.is_file():
            paths.append(path)
    return tuple(paths)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Base Git revision")
    parser.add_argument("--head", required=True, help="Head Git revision")
    parser.add_argument(
        "--ruff",
        default="ruff",
        help="Ruff executable to invoke (default: ruff)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check formatting without modifying files",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List changed Python files and do not invoke Ruff",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    files = changed_python_files(args.base, args.head)

    if args.list:
        for path in files:
            print(path.as_posix())
        return 0

    if not files:
        print("No changed Python files require formatting checks.")
        return 0

    command = [args.ruff, "format"]
    if args.check:
        command.append("--check")
    command.append("--force-exclude")
    command.extend(path.as_posix() for path in files)

    print(f"Checking Ruff formatting for {len(files)} changed Python file(s).")
    subprocess.run(command, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
