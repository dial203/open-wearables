#!/usr/bin/env python3
"""Re-derive ``data_source.device_type`` for rows stored before the source label was read.

``device_type`` used to be inferred from ``device_model`` plus the canonical *brand*.
The brand ("Apple", "Samsung") never identifies a device, and Apple's ``device_model``
comes from HealthKit's ``productType``, which reports the handset that synced a batch
rather than the device that recorded it - so a watch's samples arrive stamped
"iPhone18,1" and the source landed on ``device_type = 'phone'``. The provider's own
label for the recorder ("Michael's Apple Watch Ultra 3", "Galaxy Ring", "Garmin HRM
600") was available at every call site and never consulted.

Ingestion now reads that label (DataSourceRepository._infer_device_type). This script
applies the same rule to rows already written, calling that method directly so the
backfill cannot drift from ingestion.

``device_type`` decides which source wins a day in the summaries endpoints, so
reclassifying changes what /summaries returns for affected users - that is the point,
but preview the diff before applying it.

Only ``device_type`` is touched. ``device_model``, ``source`` and ``original_source_name``
are the captured record of what the provider reported and are never rewritten. Re-running
is a no-op once applied, since rows already matching their derived type are skipped.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/reclassify_data_source_device_type.py
    docker compose exec app uv run python scripts/data_migrations/reclassify_data_source_device_type.py --apply

Options:
    --provider apple   Limit to one provider (default: every provider)
    --user <uuid>      Limit to one user
"""

import argparse
from collections import Counter
from uuid import UUID

from sqlalchemy import select

from app.database import SessionLocal
from app.models import DataSource
from app.repositories.data_source_repository import DataSourceRepository


def _label(value: str | None) -> str:
    """Render a nullable column so an empty string is distinguishable from NULL."""
    if value is None:
        return "NULL"
    return value if value else "''"


def main(dry_run: bool, provider: str | None, user_id: UUID | None) -> None:
    repo = DataSourceRepository()

    with SessionLocal() as db:
        stmt = select(DataSource)
        if provider:
            stmt = stmt.where(DataSource.provider == provider)
        if user_id:
            stmt = stmt.where(DataSource.user_id == user_id)
        sources = list(db.scalars(stmt).all())

        if not sources:
            print("No data sources matched — nothing to do.")
            return

        changes: list[tuple[DataSource, str, str]] = []
        for ds in sources:
            derived = repo._infer_device_type(ds.device_model, ds.original_source_name, ds.source).value
            if derived != ds.device_type:
                changes.append((ds, ds.device_type or "NULL", derived))

        print(f"Scanned {len(sources)} data source(s); {len(changes)} would change.\n")
        if not changes:
            print("Every source already carries its derived device type — nothing to do.")
            return

        width_source = max(len(_label(ds.source)) for ds, _, _ in changes)
        width_model = max(len(_label(ds.device_model)) for ds, _, _ in changes)
        header = f"  {'provider':<9} {'source':<{width_source}} {'model':<{width_model}} {'from':>9}  ->  {'to':<9}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for ds, before, after in changes:
            print(
                f"  {ds.provider:<9} {_label(ds.source):<{width_source}} "
                f"{_label(ds.device_model):<{width_model}} {before:>9}  ->  {after:<9}"
            )

        tally = Counter((before, after) for _, before, after in changes)
        print("\n  Summary")
        for (before, after), count in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f"    {before:>9}  ->  {after:<9} {count} row(s)")

        if dry_run:
            print("\nPreview only — no changes made. Re-run with --apply to write them.")
            return

        for ds, _, after in changes:
            ds.device_type = after
        db.commit()
        print(f"\nUpdated {len(changes)} data source(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: preview only)")
    parser.add_argument("--provider", help="Limit to a single provider, e.g. apple")
    parser.add_argument("--user", type=UUID, help="Limit to a single user id")
    args = parser.parse_args()
    main(dry_run=not args.apply, provider=args.provider, user_id=args.user)
