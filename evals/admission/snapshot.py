"""Capture authorized policy inputs in a PostgreSQL read-only transaction.

Run on an authorized host. Supply connection details through the environment.
The output file is private and must remain outside the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.models import MemoryItem
from engram.promotion import _config, load_promotion_support
from engram.promotion_readiness import active_jobs_for_items, item_job_state
from evals.admission.dataset import Dataset, Sample, build_dataset, select_samples
from evals.admission.policy import ConfigSnapshot, PolicyInput, Receipt
from evals.admission.report import report
from evals.admission.schema import Sampling


async def capture(
    url: str,
    tenant: uuid.UUID,
    principal: uuid.UUID,
    key: bytes,
    code_sha: str,
    sampling: Sampling,
    dataset_id: str,
    dataset_version: str,
) -> Dataset:
    if len(key) < 32:
        raise ValueError("snapshot_key_too_short")
    engine = create_async_engine(url, echo=False, hide_parameters=True)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            await connection.execute(
                text(
                    "SELECT set_config('app.tenant_id', :t, true), "
                    "set_config('app.principal_id', :p, true)"
                ),
                {"t": str(tenant), "p": str(principal)},
            )
            at = (await connection.execute(text("SELECT transaction_timestamp()"))).scalar_one()
            async with AsyncSession(bind=connection, autoflush=False) as session:
                config_row = await _config(session, str(tenant))
                config = (
                    ConfigSnapshot.model_validate(
                        {field: getattr(config_row, field) for field in ConfigSnapshot.model_fields}
                    )
                    if config_row
                    else None
                )
                items = list(
                    (
                        await session.scalars(
                            select(MemoryItem)
                            .where(
                                MemoryItem.tenant_id == tenant,
                                MemoryItem.valid_to.is_(None),
                                MemoryItem.review_status == "proposed",
                            )
                            .order_by(MemoryItem.id)
                        )
                    ).all()
                )
                samples = []
                for item in items:
                    support = (await load_promotion_support(session, [item]))[item.id]
                    jobs = await active_jobs_for_items(
                        session, tenant_id=tenant, item_ids=[item.id], now=at
                    )
                    sample_id = hmac.new(key, str(item.id).encode(), hashlib.sha256).hexdigest()
                    run = support.classification_run
                    receipt = None
                    if run:
                        receipt_values = {
                            field: getattr(run, field)
                            for field in Receipt.model_fields
                            if field != "binding_matches"
                        }
                        receipt_values["binding_matches"] = (
                            run.tenant_id == item.tenant_id and run.memory_item_id == item.id
                        )
                        receipt = Receipt.model_validate(receipt_values)
                    values = {
                        field: getattr(item, field)
                        for field in (
                            "content_hash",
                            "source_type",
                            "kind",
                            "review_status",
                            "created_at",
                            "memory_confidence",
                            "source_confidence_prior",
                            "retention_confidence",
                            "retention_disposition",
                            "retention_evidence_at",
                            "conflict_resolution_status",
                        )
                    }
                    policy = PolicyInput.model_validate(
                        {
                            **values,
                            "sample_id": sample_id,
                            "live": item.valid_to is None,
                            "superseded": item.superseded_by is not None,
                            "kind_enabled": bool(support.kind and support.kind.enabled),
                            "kind_auto_promote": bool(
                                support.kind and support.kind.auto_promote_from_inferred
                            ),
                            "external_dispute": support.has_external_dispute,
                            "external_noise": support.has_external_noise_feedback,
                            "receipt": receipt,
                            "job_state": item_job_state(jobs.get(item.id, [])),
                            "recalled": "unknown",
                        }
                    )
                    samples.append(
                        Sample(sample_id=sample_id, content=None, policy_input=policy, label=None)
                    )
                selected, counts = select_samples(tuple(samples), config, at, sampling)
                return build_dataset(
                    selected,
                    config=config,
                    at=at,
                    code_sha=code_sha,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    privacy="private_dogfood",
                    sampling=sampling,
                    population_count=len(items),
                    counts=counts,
                )
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", type=uuid.UUID, required=True)
    parser.add_argument("--principal", type=uuid.UUID, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", required=True)
    parser.add_argument("--sampling", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        target = args.output.resolve()
        repo = Path(__file__).resolve().parents[2]
        if target.is_relative_to(repo):
            raise ValueError("private_output_must_be_outside_repository")
        dataset = asyncio.run(
            capture(
                os.environ["ENGRAM_DATABASE_URL"],
                args.tenant,
                args.principal,
                bytes.fromhex(os.environ["ENGRAM_EVAL_SNAPSHOT_KEY"]),
                args.code_sha,
                Sampling.model_validate_json(args.sampling.read_bytes()),
                args.dataset_id,
                args.dataset_version,
            )
        )
        # Exclusive creation prevents overwriting an artifact or following a symlink.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(dataset.model_dump_json(indent=2))
        print(json.dumps(report(dataset), sort_keys=True, indent=2))
    except Exception:
        print('{"error":"snapshot_failed_no_private_details"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
