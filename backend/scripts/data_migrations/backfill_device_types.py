#!/usr/bin/env python3
"""One-off: re-resolve ``data_source.device_type`` for rows written before the new mappings.

Applies the same rule as live sync (``DataSourceRepository.next_device_type``): cloud-provider
rows are recomputed outright, which also corrects types the old keywords got wrong (e.g. Garmin
Index BPM stored as scale); SDK-provider rows only upgrade NULL/``other`` to a concrete type.
iPad rows stored as phone move to tablet: that phone came from the same productType, not the SDK.
Later mapping changes reach existing rows on their next sync, so this only needs to run once.

Fork: resolved with the same classifier live sync uses (``DataSourceRepository._infer_device_type``)
rather than the bare ``infer_device_type``, so a sensor declared on the connection still decides the
type. Upstream's classifier never sees that declaration; run as written, it would reclassify every
Strava source a person had marked as recorded with an ECG strap from chest_strap back to watch -
the reference instrument demoted below the wrist sensors it is there to be compared against.

Idempotent: a second run finds nothing to change. Safe to run on every startup until removed.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/backfill_device_types.py --dry-run
    docker compose exec app uv run python scripts/data_migrations/backfill_device_types.py
"""

import argparse
from collections import Counter
from uuid import UUID

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DataSource
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import DeviceType, ProviderName


def backfill_device_types(db: Session, *, dry_run: bool) -> Counter[str]:
    """Re-resolve device types. Does not commit — the caller owns the transaction.

    Returns a count of changes keyed by ``"<provider>: <old> -> <new>"``.
    """
    changes: Counter[str] = Counter()
    repo = DataSourceRepository()
    # One lookup per account, not per row: a declaration belongs to the connection.
    declared: dict[tuple[UUID, ProviderName, UUID | None], str | None] = {}
    for ds in db.query(DataSource).all():
        try:
            provider = ProviderName(ds.provider)
        except ValueError:
            continue
        account = (ds.user_id, provider, ds.user_connection_id)
        if account not in declared:
            declared[account] = repo._connection_sensor_label(db, *account)
        resolved = repo._infer_device_type(
            ds.device_model,
            ds.original_source_name,
            ds.source,
            declared[account],
            provider=provider,
        )
        # Google formFactor isn't stored, so recomputing here would undo it; live sync recomputes Google rows
        if provider == ProviderName.GOOGLE_HEALTH and ds.device_type not in (None, DeviceType.OTHER):
            continue
        new_type = DataSourceRepository.next_device_type(provider, ds.device_type, resolved)
        if (
            ds.device_type == DeviceType.PHONE
            and resolved == DeviceType.TABLET
            and (ds.device_model or "").startswith("iPad")
        ):
            new_type = DeviceType.TABLET.value
        if new_type == ds.device_type:
            continue
        changes[f"{provider.value}: {ds.device_type} -> {new_type}"] += 1
        if not dry_run:
            ds.device_type = new_type

    verb = "would change" if dry_run else "changed"
    for change, count in sorted(changes.items()):
        print(f"{change:<40} {verb} {count} row(s)")
    if dry_run:
        print("\nDry run — no changes made.")
    return changes


def main(dry_run: bool) -> None:
    with SessionLocal() as db:
        changes = backfill_device_types(db, dry_run=dry_run)
        if dry_run:
            return
        if not changes:
            print("Nothing to do — all device types are current.")
            return
        db.commit()
        print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview affected rows without modifying data")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
