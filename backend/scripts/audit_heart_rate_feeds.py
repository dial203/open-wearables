#!/usr/bin/env python3
"""Audit stored heart rate for feeds that overwrite or mix with each other. Read-only.

Each heart-rate row records the feed that wrote it (data_point_series.route_id; see
app/schemas/enums/sample_route.py). Before that, two feeds of one provider could share
a data source and overwrite each other at every second they both had - a Garmin run
came back with all-day one-minute averages on every :00/:15/:30/:45 second - and
nothing on the row showed it. This script checks what the database holds, for every
account and every source, over a window:

  workout spans     Inside a recorded workout, a data source that holds the workout's
                    own trace holds nothing else. A row from another feed there is a
                    VIOLATION. A row with no feed recorded is LEGACY: written before
                    feeds were recorded, and gone once the provider delivers that
                    workout again.
  overlaps          No two workouts overlap on one data source. Two recorders filed on
                    one source share its series. VIOLATION.
  FIT fidelity      (--fit) For Garmin workouts whose FIT file was kept
                    (STORE_FIT_FILES), the stored heart rate equals the file's record
                    heart rate at every second both have. Any difference is a VIOLATION.
                    This is the ground truth: the watch's own recording, value for value.
  untagged rows     Heart-rate rows with no feed recorded, per source. Informational.
  shared series     Sources whose heart-rate series holds more than one feed in the
                    window (Oura's /heartrate and sleep series, Polar's continuous and
                    sleep HR). Informational: expected, and why readers should filter on
                    `route_kind` from /timeseries rather than treat a source as one feed.

Exit status is 1 when any VIOLATION is found, so it can run on a schedule.

Usage (inside Docker):
    docker compose exec app uv run python scripts/audit_heart_rate_feeds.py --days 30
    docker compose exec app uv run python scripts/audit_heart_rate_feeds.py --days 30 --fit --json
    docker compose exec app uv run python scripts/audit_heart_rate_feeds.py --user <uuid> --start 2026-10-05
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.schemas.enums import ROUTE_BY_ID, ROUTE_KIND_BY_ROUTE, RouteKind, SeriesType, get_series_type_id

HR_TYPE = get_series_type_id(SeriesType.heart_rate)
WORKOUT_ROUTE_IDS = sorted(
    route_id for route_id, route in ROUTE_BY_ID.items() if ROUTE_KIND_BY_ROUTE[route] is RouteKind.WORKOUT
)


@dataclass
class SpanFinding:
    workout_id: str
    external_id: str | None
    provider: str
    data_source_id: str
    start: str
    end: str
    other_feed_rows: int
    other_feeds: list[str]
    untagged_rows: int


@dataclass
class OverlapFinding:
    data_source_id: str
    provider: str
    first_workout: str | None
    second_workout: str | None
    overlap_seconds: int


@dataclass
class FitFinding:
    workout_id: str
    external_id: str | None
    shared_seconds: int
    identical_seconds: int
    differing_seconds: int
    only_in_fit: int
    only_stored: int
    first_differences: list[tuple[str, float, float]] = field(default_factory=list)


@dataclass
class AuditReport:
    start: str
    end: str
    user_id: str | None
    workout_spans: list[SpanFinding] = field(default_factory=list)
    overlaps: list[OverlapFinding] = field(default_factory=list)
    fit: list[FitFinding] = field(default_factory=list)
    fit_checked: int = 0
    fit_unavailable: int = 0
    untagged_rows: list[dict[str, Any]] = field(default_factory=list)
    shared_series: list[dict[str, Any]] = field(default_factory=list)

    @property
    def violations(self) -> int:
        return (
            sum(1 for s in self.workout_spans if s.other_feed_rows)
            + len(self.overlaps)
            + sum(1 for f in self.fit if f.differing_seconds)
        )


_USER = "AND ds.user_id = :user_id"

_WORKOUT_SPANS = """
    WITH w AS (
        SELECT er.id, er.external_id, er.start_datetime AS s, er.end_datetime AS e,
               ds.user_id, ds.provider, ds.user_connection_id
        FROM event_record er
        JOIN data_source ds ON ds.id = er.data_source_id
        WHERE er.category = 'workout' AND er.start_datetime < :end AND er.end_datetime > :start {user}
    ),
    owned AS (
        -- the account's sources that hold the workout's own trace inside its span
        SELECT DISTINCT w.id AS workout_id, p.data_source_id
        FROM w
        JOIN data_source sds
          ON sds.user_id = w.user_id AND sds.provider = w.provider
         AND sds.user_connection_id IS NOT DISTINCT FROM w.user_connection_id
        JOIN data_point_series p
          ON p.data_source_id = sds.id AND p.series_type_definition_id = :hr
         AND p.recorded_at BETWEEN w.s AND w.e AND p.route_id IN :workout_routes
    )
    SELECT w.id, w.external_id, w.provider, o.data_source_id, w.s, w.e,
           COUNT(*) FILTER (WHERE p.route_id IS NOT NULL) AS other_rows,
           ARRAY_AGG(DISTINCT p.route_id) FILTER (WHERE p.route_id IS NOT NULL) AS other_routes,
           COUNT(*) FILTER (WHERE p.route_id IS NULL) AS untagged_rows
    FROM owned o
    JOIN w ON w.id = o.workout_id
    JOIN data_point_series p
      ON p.data_source_id = o.data_source_id AND p.series_type_definition_id = :hr
     AND p.recorded_at BETWEEN w.s AND w.e
     AND (p.route_id IS NULL OR p.route_id NOT IN :workout_routes)
    GROUP BY w.id, w.external_id, w.provider, o.data_source_id, w.s, w.e
    ORDER BY w.s
"""

_OVERLAPS = """
    SELECT a.data_source_id, ds.provider, a.external_id, b.external_id,
           EXTRACT(EPOCH FROM LEAST(a.end_datetime, b.end_datetime) - GREATEST(a.start_datetime, b.start_datetime))
    FROM event_record a
    JOIN event_record b
      ON b.data_source_id = a.data_source_id AND b.category = a.category AND a.id < b.id
     AND b.start_datetime < a.end_datetime AND b.end_datetime > a.start_datetime
     AND b.external_id IS DISTINCT FROM a.external_id
    JOIN data_source ds ON ds.id = a.data_source_id
    WHERE a.category = 'workout' AND a.start_datetime < :end AND a.end_datetime > :start {user}
    ORDER BY a.start_datetime
"""

_UNTAGGED = """
    SELECT ds.provider, ds.device_model, ds.source, p.data_source_id, COUNT(*)
    FROM data_point_series p
    JOIN data_source ds ON ds.id = p.data_source_id
    WHERE p.series_type_definition_id = :hr AND p.route_id IS NULL
      AND p.recorded_at >= :start AND p.recorded_at < :end {user}
    GROUP BY ds.provider, ds.device_model, ds.source, p.data_source_id
    ORDER BY COUNT(*) DESC
"""

_SHARED = """
    SELECT ds.provider, ds.device_model, ds.source, p.data_source_id,
           ARRAY_AGG(DISTINCT p.route_id) FILTER (WHERE p.route_id IS NOT NULL)
    FROM data_point_series p
    JOIN data_source ds ON ds.id = p.data_source_id
    WHERE p.series_type_definition_id = :hr AND p.recorded_at >= :start AND p.recorded_at < :end {user}
    GROUP BY ds.provider, ds.device_model, ds.source, p.data_source_id
    HAVING COUNT(DISTINCT p.route_id) > 1
"""

_GARMIN_FIT_WORKOUTS = """
    SELECT er.id, er.external_id, er.start_datetime, er.end_datetime, ds.user_id, ds.user_connection_id,
           wd.fit_file_key
    FROM event_record er
    JOIN data_source ds ON ds.id = er.data_source_id
    JOIN workout_details wd ON wd.record_id = er.id
    WHERE er.category = 'workout' AND ds.provider = 'garmin' AND wd.fit_file_key IS NOT NULL
      AND er.start_datetime < :end AND er.end_datetime > :start {user}
    ORDER BY er.start_datetime
"""

_STORED_WORKOUT_HR = """
    SELECT p.recorded_at, p.value
    FROM data_point_series p
    JOIN data_source ds ON ds.id = p.data_source_id
    WHERE ds.user_id = :user_id AND ds.provider = 'garmin'
      AND ds.user_connection_id IS NOT DISTINCT FROM :connection_id
      AND p.series_type_definition_id = :hr AND p.route_id IN :workout_routes
      AND p.recorded_at BETWEEN :s AND :e
"""


def _sql(template: str, user_id: UUID | None, expanding: tuple[str, ...] = ()) -> Any:
    stmt = text(template.format(user=_USER if user_id else ""))
    for name in expanding:
        stmt = stmt.bindparams(bindparam(name, expanding=True))
    return stmt


def _route_names(route_ids: list[int] | None) -> list[str]:
    return sorted(ROUTE_BY_ID[r].value if r in ROUTE_BY_ID else f"unknown:{r}" for r in (route_ids or []))


def _fit_heart_rate(fit_bytes: bytes) -> dict[int, float]:
    # Imported here so the rest of the audit runs where fitdecode is unavailable.
    from app.services.fit_parser import parse_fit_file

    result = parse_fit_file(fit_bytes, UUID(int=0))
    return {
        int(s.recorded_at.timestamp()): float(s.value) for s in result.samples if s.series_type is SeriesType.heart_rate
    }


def audit(
    db: Session,
    start: datetime,
    end: datetime,
    user_id: UUID | None = None,
    check_fit: bool = False,
    fit_loader: Any = None,
) -> AuditReport:
    params: dict[str, Any] = {"start": start, "end": end, "hr": HR_TYPE, "workout_routes": WORKOUT_ROUTE_IDS}
    if user_id:
        params["user_id"] = user_id
    report = AuditReport(start=start.isoformat(), end=end.isoformat(), user_id=str(user_id) if user_id else None)

    for row in db.execute(_sql(_WORKOUT_SPANS, user_id, ("workout_routes",)), params):
        report.workout_spans.append(
            SpanFinding(
                workout_id=str(row[0]),
                external_id=row[1],
                provider=str(row[2]),
                data_source_id=str(row[3]),
                start=row[4].isoformat(),
                end=row[5].isoformat(),
                other_feed_rows=int(row[6]),
                other_feeds=_route_names(row[7]),
                untagged_rows=int(row[8]),
            )
        )

    for row in db.execute(_sql(_OVERLAPS, user_id), params):
        report.overlaps.append(
            OverlapFinding(
                data_source_id=str(row[0]),
                provider=str(row[1]),
                first_workout=row[2],
                second_workout=row[3],
                overlap_seconds=int(row[4] or 0),
            )
        )

    for row in db.execute(_sql(_UNTAGGED, user_id), params):
        report.untagged_rows.append(
            {
                "provider": str(row[0]),
                "device_model": row[1],
                "source": row[2],
                "data_source_id": str(row[3]),
                "rows": int(row[4]),
            }
        )

    for row in db.execute(_sql(_SHARED, user_id), params):
        report.shared_series.append(
            {
                "provider": str(row[0]),
                "device_model": row[1],
                "source": row[2],
                "data_source_id": str(row[3]),
                "feeds": _route_names(row[4]),
            }
        )

    if check_fit:
        if fit_loader is None:
            from app.services.raw_payload_storage import get_fit_file as fit_loader
        stored_sql = _sql(_STORED_WORKOUT_HR, None, ("workout_routes",))
        for row in db.execute(_sql(_GARMIN_FIT_WORKOUTS, user_id), params):
            workout_id, external_id, s, e, owner, connection_id, key = row
            fit_bytes = fit_loader(key)
            if not fit_bytes:
                report.fit_unavailable += 1
                continue
            report.fit_checked += 1
            fit_hr = _fit_heart_rate(fit_bytes)
            stored = {
                int(r[0].timestamp()): float(r[1])
                for r in db.execute(
                    stored_sql,
                    {
                        "user_id": owner,
                        "connection_id": connection_id,
                        "hr": HR_TYPE,
                        "workout_routes": WORKOUT_ROUTE_IDS,
                        "s": s,
                        "e": e,
                    },
                )
            }
            shared = sorted(set(fit_hr) & set(stored))
            differing = [t for t in shared if fit_hr[t] != stored[t]]
            report.fit.append(
                FitFinding(
                    workout_id=str(workout_id),
                    external_id=external_id,
                    shared_seconds=len(shared),
                    identical_seconds=len(shared) - len(differing),
                    differing_seconds=len(differing),
                    only_in_fit=len(set(fit_hr) - set(stored)),
                    only_stored=len(set(stored) - set(fit_hr)),
                    first_differences=[
                        (datetime.fromtimestamp(t, UTC).isoformat(), fit_hr[t], stored[t]) for t in differing[:5]
                    ],
                )
            )
    return report


def _print(report: AuditReport) -> None:
    print(
        f"Heart-rate feed audit {report.start} .. {report.end}"
        + (f" for user {report.user_id}" if report.user_id else "")
    )
    mixed = [s for s in report.workout_spans if s.other_feed_rows]
    legacy = [s for s in report.workout_spans if not s.other_feed_rows and s.untagged_rows]
    print(f"\nWorkout spans holding another feed: {len(mixed)}  (VIOLATION)")
    for s in mixed[:20]:
        feeds = ", ".join(s.other_feeds)
        print(f"  {s.provider:10} {s.start}  workout {s.external_id}  {s.other_feed_rows} rows from {feeds}")
    print(f"Workout spans with rows from before feeds were recorded: {len(legacy)}  (re-sync to clear)")
    print(f"\nOverlapping workouts on one data source: {len(report.overlaps)}  (VIOLATION)")
    for o in report.overlaps[:20]:
        print(f"  {o.provider:10} {o.first_workout} / {o.second_workout}  {o.overlap_seconds}s on {o.data_source_id}")
    if report.fit_checked or report.fit_unavailable:
        bad = [f for f in report.fit if f.differing_seconds]
        print(
            f"\nGarmin workouts checked against their FIT file: {report.fit_checked}"
            f" ({report.fit_unavailable} with no stored file);  differing: {len(bad)}  (VIOLATION)"
        )
        for f in report.fit:
            pct = 100.0 * f.identical_seconds / f.shared_seconds if f.shared_seconds else 0.0
            print(
                f"  workout {f.external_id}: {f.identical_seconds}/{f.shared_seconds} identical ({pct:.1f}%),"
                f" {f.only_in_fit} only in FIT, {f.only_stored} only stored"
            )
    total_untagged = sum(u["rows"] for u in report.untagged_rows)
    print(f"\nHeart-rate rows with no feed recorded: {total_untagged} across {len(report.untagged_rows)} sources")
    print(f"Sources whose series holds more than one feed: {len(report.shared_series)}")
    for s in report.shared_series[:20]:
        print(f"  {s['provider']:10} {s['device_model'] or ''} {s['source'] or ''}: {', '.join(s['feeds'])}")
    print(f"\n{report.violations} violation(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="window ending now (default 30)")
    parser.add_argument("--start", help="window start, ISO date or datetime (overrides --days)")
    parser.add_argument("--end", help="window end, ISO date or datetime (default now)")
    parser.add_argument("--user", type=UUID, help="restrict to one OpenWearables user id")
    parser.add_argument("--fit", action="store_true", help="compare Garmin workouts with their stored FIT files")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    def parse(value: str) -> datetime:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    end = parse(args.end) if args.end else datetime.now(UTC)
    start = parse(args.start) if args.start else end - timedelta(days=args.days)
    with SessionLocal() as db:
        report = audit(db, start, end, args.user, args.fit)
    if args.json:
        print(json.dumps({**asdict(report), "violations": report.violations}, indent=2, default=str))
    else:
        _print(report)
    return 1 if report.violations else 0


if __name__ == "__main__":
    sys.exit(main())
