#!/usr/bin/env python3
"""Relabel Apple Watch sleeping wrist temperature stored as body_temperature to skin_temperature.

HealthKit has two temperature types the Apple route ingests. HKQuantityTypeIdentifierBodyTemperature
is a thermometer or manual reading of core temperature. HKQuantityTypeIdentifierAppleSleepingWristTemperature
is the watch's overnight wrist skin temperature, a few degrees lower and a different construct. The SDK
metric map (app/constants/series_types/sdk/metric_types.py), shared by the mobile SDK and the Apple
Health XML import, filed both under body_temperature (id=45). The mapping now sends wrist temperature to
skin_temperature (id=46); this script moves the rows written before that.

Telling them apart: a stored row does not keep its HealthKit type identifier. external_id is the
sample's UUID, route_id only says "sdk.records" / "apple_xml.records", and provider_metadata exists
only on SDK rows synced since 2026-09-23 (XML rows keep it for HRV alone). What does survive is the
data source: Apple's watch software is the only writer of AppleSleepingWristTemperature, and it writes
no BodyTemperature, while thermometer apps and manual entries write from a phone or name no hardware.
So a body_temperature row on an Apple source whose provider-reported hardware is an Apple Watch is
wrist temperature:

- provider = 'apple' (SDK and XML import alike);
- device_model is Apple Watch hardware: a product type such as "Watch7,5" (SDK, from
  HKSourceRevision.productType) or the bare HKDevice model "Watch" (XML import);
- device_model_origin is not 'label', i.e. the model was named on the payload rather than filled
  in from the account's device label.

What this leaves alone, on purpose: Apple body_temperature rows on any other source (thermometers,
manual entries, a source with no hardware code). The rule's one blind spot is a third-party watchOS
app writing BodyTemperature from the watch itself, which would be moved too. --dry-run reports how
many matched rows sit on a source not named as an Apple Watch (an app on the watch, a renamed watch,
or an XML import from before sources were keyed on the writer) and the value range of the matched
rows, so that can be checked before applying: wrist temperature sits around 33-36 °C.

Conflict handling: after the mapping change, a re-sync of the same HealthKit sample (e.g. after
resetAnchors()) writes a skin_temperature row at the same (data_source, recorded_at) as the old
body_temperature row, which would collide with the unique constraint on relabel. That stale duplicate
is deleted rather than relabeled; the skin_temperature row already holds the same sample.

Idempotent: once relabeled, rows no longer match the body_temperature filter, so re-runs are no-ops.
Not wired into startup (scripts/start/app.sh): the rule is inferred from how ingestion stores rows,
so run --dry-run against the deployment first.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/relabel_apple_wrist_temp_to_skin_temp.py --dry-run
    docker compose exec app uv run python scripts/data_migrations/relabel_apple_wrist_temp_to_skin_temp.py
"""

import argparse

from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from app.database import SessionLocal

PROVIDER = "apple"
BODY_TEMP_ID = 45
SKIN_TEMP_ID = 46
# Apple Watch hardware as a data source records it: "Watch7,5" from the SDK, "Watch" from the XML export.
WATCH_MODEL_PATTERN = r"^Watch([0-9]+,[0-9]+)?$"
LABEL_ORIGIN = "label"

_PARAMS = {
    "provider": PROVIDER,
    "body": BODY_TEMP_ID,
    "skin": SKIN_TEMP_ID,
    "watch_model": WATCH_MODEL_PATTERN,
    "label": LABEL_ORIGIN,
}

# An Apple source whose provider-reported hardware is an Apple Watch. Expects the data_source alias "ds".
_WATCH_SOURCE = """
    ds.provider = :provider
    AND ds.device_model ~ :watch_model
    AND ds.device_model_origin IS DISTINCT FROM :label
"""

# data_point_series: unique on (data_source_id, series_type_definition_id, recorded_at)
_SERIES_COUNT = text(f"""
    SELECT COUNT(*)
    FROM data_point_series dps
    JOIN data_source ds ON ds.id = dps.data_source_id
    WHERE {_WATCH_SOURCE}
      AND dps.series_type_definition_id = :body
""")

# body_temperature rows that collide with an already-correct skin_temperature row -
# these get deleted, not relabeled.
_SERIES_CONFLICT_COUNT = text(f"""
    SELECT COUNT(*)
    FROM data_point_series dps
    JOIN data_source ds ON ds.id = dps.data_source_id
    WHERE {_WATCH_SOURCE}
      AND dps.series_type_definition_id = :body
      AND EXISTS (
          SELECT 1 FROM data_point_series e
          WHERE e.data_source_id = dps.data_source_id
            AND e.series_type_definition_id = :skin
            AND e.recorded_at = dps.recorded_at
      )
""")

_SERIES_UPDATE = text(f"""
    UPDATE data_point_series dps
    SET series_type_definition_id = :skin
    FROM data_source ds
    WHERE ds.id = dps.data_source_id
      AND {_WATCH_SOURCE}
      AND dps.series_type_definition_id = :body
      AND NOT EXISTS (
          SELECT 1 FROM data_point_series e
          WHERE e.data_source_id = dps.data_source_id
            AND e.series_type_definition_id = :skin
            AND e.recorded_at = dps.recorded_at
      )
""")

# Any matching body_temperature rows still present collided with an existing
# skin_temperature row above. The EXISTS guard ensures we only delete confirmed
# duplicates - rows without a skin_temperature counterpart (e.g. written by an old pod
# during a rolling deploy) are left for the next run to pick up via _SERIES_UPDATE.
_SERIES_DELETE_DUPLICATES = text(f"""
    DELETE FROM data_point_series dps
    USING data_source ds
    WHERE ds.id = dps.data_source_id
      AND {_WATCH_SOURCE}
      AND dps.series_type_definition_id = :body
      AND EXISTS (
          SELECT 1 FROM data_point_series e
          WHERE e.data_source_id = dps.data_source_id
            AND e.series_type_definition_id = :skin
            AND e.recorded_at = dps.recorded_at
      )
""")

# Dry-run review figures: what the rule matches, and what it leaves behind.
_SERIES_REVIEW = text(f"""
    SELECT
        COUNT(*) FILTER (
            WHERE COALESCE(ds.original_source_name, ds.source, '') NOT ILIKE '%apple watch%'
        ) AS not_named_as_watch,
        MIN(dps.value) AS min_value,
        AVG(dps.value) AS avg_value,
        MAX(dps.value) AS max_value
    FROM data_point_series dps
    JOIN data_source ds ON ds.id = dps.data_source_id
    WHERE {_WATCH_SOURCE}
      AND dps.series_type_definition_id = :body
""")

_SERIES_LEFT_COUNT = text(f"""
    SELECT COUNT(*)
    FROM data_point_series dps
    JOIN data_source ds ON ds.id = dps.data_source_id
    WHERE ds.provider = :provider
      AND dps.series_type_definition_id = :body
      AND NOT ({_WATCH_SOURCE})
""")

# data_point_series_archive: unique on (data_source_id, series_type_definition_id,
# bucket_start_at, aggregation_type)
_ARCHIVE_COUNT = text(f"""
    SELECT COUNT(*)
    FROM data_point_series_archive a
    JOIN data_source ds ON ds.id = a.data_source_id
    WHERE {_WATCH_SOURCE}
      AND a.series_type_definition_id = :body
""")

_ARCHIVE_CONFLICT_COUNT = text(f"""
    SELECT COUNT(*)
    FROM data_point_series_archive a
    JOIN data_source ds ON ds.id = a.data_source_id
    WHERE {_WATCH_SOURCE}
      AND a.series_type_definition_id = :body
      AND EXISTS (
          SELECT 1 FROM data_point_series_archive e
          WHERE e.data_source_id = a.data_source_id
            AND e.series_type_definition_id = :skin
            AND e.bucket_start_at = a.bucket_start_at
            AND e.aggregation_type = a.aggregation_type
      )
""")

_ARCHIVE_UPDATE = text(f"""
    UPDATE data_point_series_archive a
    SET series_type_definition_id = :skin
    FROM data_source ds
    WHERE ds.id = a.data_source_id
      AND {_WATCH_SOURCE}
      AND a.series_type_definition_id = :body
      AND NOT EXISTS (
          SELECT 1 FROM data_point_series_archive e
          WHERE e.data_source_id = a.data_source_id
            AND e.series_type_definition_id = :skin
            AND e.bucket_start_at = a.bucket_start_at
            AND e.aggregation_type = a.aggregation_type
      )
""")

_ARCHIVE_DELETE_DUPLICATES = text(f"""
    DELETE FROM data_point_series_archive a
    USING data_source ds
    WHERE ds.id = a.data_source_id
      AND {_WATCH_SOURCE}
      AND a.series_type_definition_id = :body
      AND EXISTS (
          SELECT 1 FROM data_point_series_archive e
          WHERE e.data_source_id = a.data_source_id
            AND e.series_type_definition_id = :skin
            AND e.bucket_start_at = a.bucket_start_at
            AND e.aggregation_type = a.aggregation_type
      )
""")


def _scalar_count(db: Session, query: TextClause) -> int:
    return db.execute(query, _PARAMS).scalar() or 0


def _print_review(db: Session) -> None:
    review = db.execute(_SERIES_REVIEW, _PARAMS).one()
    if review.min_value is not None:
        print(
            f"  matched values:          {review.min_value:.2f} to {review.max_value:.2f} °C "
            f"(mean {review.avg_value:.2f}; wrist temperature sits around 33-36 °C)"
        )
    print(f"  on sources not named as an Apple Watch (review): {review.not_named_as_watch}")
    print(f"  Apple body_temperature rows left as they are:   {_scalar_count(db, _SERIES_LEFT_COUNT)}")


def relabel_apple_wrist_temp(db: Session, *, dry_run: bool) -> dict[str, int]:
    """Relabel Apple Watch body_temperature rows to skin_temperature. Does not commit -
    caller owns the transaction.

    In dry-run mode counts come from up-front SELECTs. In live mode counts come from
    the actual rowcount returned by each DML statement, so reported numbers reflect
    what was truly written. Rows that collide with an already-correct skin_temperature
    row are deleted rather than relabeled.
    """
    if dry_run:
        series_deleted = _scalar_count(db, _SERIES_CONFLICT_COUNT)
        series_updated = _scalar_count(db, _SERIES_COUNT) - series_deleted
        archive_deleted = _scalar_count(db, _ARCHIVE_CONFLICT_COUNT)
        archive_updated = _scalar_count(db, _ARCHIVE_COUNT) - archive_deleted
        print(f"data_point_series:         Would relabel {series_updated}, remove {series_deleted} duplicate(s)")
        _print_review(db)
        print(f"data_point_series_archive: Would relabel {archive_updated}, remove {archive_deleted} duplicate(s)")
        print("\nDry run — no changes made.")
        return {
            "series_updated": series_updated,
            "series_deleted": series_deleted,
            "archive_updated": archive_updated,
            "archive_deleted": archive_deleted,
        }

    series_updated = db.execute(_SERIES_UPDATE, _PARAMS).rowcount  # ty: ignore[unresolved-attribute]
    series_deleted = db.execute(_SERIES_DELETE_DUPLICATES, _PARAMS).rowcount  # ty: ignore[unresolved-attribute]
    archive_updated = db.execute(_ARCHIVE_UPDATE, _PARAMS).rowcount  # ty: ignore[unresolved-attribute]
    archive_deleted = db.execute(_ARCHIVE_DELETE_DUPLICATES, _PARAMS).rowcount  # ty: ignore[unresolved-attribute]

    print(f"data_point_series:         Relabeled {series_updated}, removed {series_deleted} duplicate(s)")
    print(f"data_point_series_archive: Relabeled {archive_updated}, removed {archive_deleted} duplicate(s)")

    return {
        "series_updated": series_updated,
        "series_deleted": series_deleted,
        "archive_updated": archive_updated,
        "archive_deleted": archive_deleted,
    }


def main(dry_run: bool) -> None:
    with SessionLocal() as db:
        result = relabel_apple_wrist_temp(db, dry_run=dry_run)
        if dry_run:
            return
        if not any(result.values()):
            print("Nothing to do — no Apple Watch body_temperature rows found.")
            return
        db.commit()
        print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview affected rows without modifying data")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
