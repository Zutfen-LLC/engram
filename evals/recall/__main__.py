"""Execute a protected, read-only recall evaluation manifest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from evals.admission.schema import digest
from evals.recall.runner import (
    evaluation_state_identity,
    read_only_evaluation_session,
    run_recall_evaluation,
    write_reports,
)
from evals.recall.runtime_config import runtime_config_digest
from evals.recall.schema import RecallEvaluationManifest


async def _run(args: argparse.Namespace) -> None:
    manifest = RecallEvaluationManifest.model_validate_json(args.manifest.read_bytes())
    database_url = os.environ["ENGRAM_DATABASE_URL"]
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with read_only_evaluation_session(session_factory, manifest) as session:
            if args.capture_state:
                print(
                    json.dumps(
                        {
                            "snapshot_digest": digest(
                                await evaluation_state_identity(session, manifest)
                            ),
                            "runtime_config_digest": runtime_config_digest(),
                        },
                        sort_keys=True,
                    )
                )
                return
            private, public = await run_recall_evaluation(session, manifest)
        write_reports(
            private_path=args.private_output,
            public_json_path=args.public_json,
            public_markdown_path=args.public_markdown,
            private=private,
            public=public,
        )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--capture-state",
        action="store_true",
        help="print the live protected-state digest for manifest construction",
    )
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--public-json", type=Path)
    parser.add_argument("--public-markdown", type=Path)
    args = parser.parse_args()
    if not args.capture_state and (args.public_json is None or args.public_markdown is None):
        parser.error("--public-json and --public-markdown are required for evaluation")
    try:
        asyncio.run(_run(args))
    except Exception:
        # Exception text can include protected query or item data.
        print('{"error":"recall_evaluation_failed_no_private_details"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
