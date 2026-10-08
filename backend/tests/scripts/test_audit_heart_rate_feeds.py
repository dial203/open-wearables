"""The heart-rate feed audit finds what it is for, and stays quiet on a clean account."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import DataPointSeries
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.schemas.enums import ProviderName, SampleRoute, SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    UserConnectionFactory,
    UserFactory,
    WorkoutDetailsFactory,
)

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_heart_rate_feeds.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_heart_rate_feeds", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_module = _load()
START = datetime(2026, 10, 6, 2, 43, 45, tzinfo=timezone.utc)
WINDOW = (START - timedelta(hours=1), START + timedelta(hours=2))


def _garmin_run(db: Session, seconds: int = 120) -> tuple[Any, Any]:
    """A Garmin account with one workout and its own per-second trace."""
    user = UserFactory()
    conn = UserConnectionFactory(user=user, provider="garmin")
    source = DataSourceFactory(
        user=user, provider=ProviderName.GARMIN, source="garmin", device_model="fenix 9 Pro", user_connection_id=conn.id
    )
    workout = EventRecordFactory(
        data_source=source, start_datetime=START, end_datetime=START + timedelta(seconds=seconds), external_id="run-1"
    )
    _write(db, user.id, conn.id, [(START + timedelta(seconds=i), 130 + i % 5) for i in range(seconds)])
    return user, (conn, source, workout)


def _write(
    db: Session,
    user_id: Any,
    connection_id: Any,
    points: list[tuple[datetime, int]],
    route: SampleRoute | None = SampleRoute.GARMIN_ACTIVITY_DETAILS,
) -> None:
    DataPointSeriesRepository(DataPointSeries).bulk_create(
        db,
        [
            TimeSeriesSampleCreate(
                id=uuid4(),
                user_id=user_id,
                provider="garmin",
                source="garmin",
                device_model="fenix 9 Pro",
                user_connection_id=connection_id,
                recorded_at=at,
                value=bpm,
                series_type=SeriesType.heart_rate,
                route=route,
            )
            for at, bpm in points
        ],
    )


def test_a_clean_workout_reports_nothing(db: Session) -> None:
    user, _ = _garmin_run(db)
    report = audit_module.audit(db, *WINDOW, user_id=user.id)

    assert report.workout_spans == []
    assert report.overlaps == []
    assert report.violations == 0


def test_another_feed_inside_a_workout_is_a_violation(db: Session) -> None:
    user, (conn, _, _) = _garmin_run(db)
    # An all-day value at a second the run has no sample of its own (an auto-pause).
    _write(db, user.id, conn.id, [(START + timedelta(seconds=200), 99)], route=SampleRoute.GARMIN_DAILIES)
    _write(
        db, user.id, conn.id, [(START + timedelta(seconds=60, milliseconds=500), 99)], route=SampleRoute.GARMIN_DAILIES
    )

    report = audit_module.audit(db, *WINDOW, user_id=user.id)

    assert [(s.other_feed_rows, s.other_feeds) for s in report.workout_spans] == [(1, ["garmin.dailies"])]
    assert report.violations == 1


def test_a_row_from_before_feeds_were_recorded_is_legacy_not_a_violation(db: Session) -> None:
    user, (conn, _, _) = _garmin_run(db)
    _write(db, user.id, conn.id, [(START + timedelta(seconds=30, milliseconds=250), 88)], route=None)

    report = audit_module.audit(db, *WINDOW, user_id=user.id)

    assert [(s.other_feed_rows, s.untagged_rows) for s in report.workout_spans] == [(0, 1)]
    assert report.violations == 0
    assert sum(u["rows"] for u in report.untagged_rows) == 1


def test_two_workouts_overlapping_on_one_source_are_a_violation(db: Session) -> None:
    user, (_, source, _) = _garmin_run(db)
    EventRecordFactory(
        data_source=source,
        start_datetime=START + timedelta(seconds=30),
        end_datetime=START + timedelta(seconds=90),
        external_id="run-2",
    )

    report = audit_module.audit(db, *WINDOW, user_id=user.id)

    assert [(o.first_workout, o.second_workout, o.overlap_seconds) for o in report.overlaps] == [("run-1", "run-2", 60)]
    assert report.violations == 1


def test_the_fit_check_compares_every_second_with_the_watchs_own_file(db: Session, monkeypatch: Any) -> None:
    user, (_, _, workout) = _garmin_run(db, seconds=60)
    WorkoutDetailsFactory(event_record=workout, fit_file_key="fit-files/garmin/x/run-1.fit")
    fit = {int((START + timedelta(seconds=i)).timestamp()): float(130 + i % 5) for i in range(60)}
    fit[int((START + timedelta(seconds=15)).timestamp())] = 99.0  # the file disagrees with what is stored here
    monkeypatch.setattr(audit_module, "_fit_heart_rate", lambda _bytes: fit)

    report = audit_module.audit(db, *WINDOW, user_id=user.id, check_fit=True, fit_loader=lambda _key: b"fit")

    (finding,) = report.fit
    assert (finding.shared_seconds, finding.identical_seconds, finding.differing_seconds) == (60, 59, 1)
    assert report.fit_checked == 1
    assert report.violations == 1


def test_a_workout_whose_fit_was_not_kept_is_counted_not_failed(db: Session) -> None:
    user, (_, _, workout) = _garmin_run(db, seconds=60)
    WorkoutDetailsFactory(event_record=workout, fit_file_key="fit-files/garmin/x/run-1.fit")

    report = audit_module.audit(db, *WINDOW, user_id=user.id, check_fit=True, fit_loader=lambda _key: None)

    assert (report.fit_checked, report.fit_unavailable, report.violations) == (0, 1, 0)


def test_a_source_holding_two_feeds_is_listed(db: Session) -> None:
    user, (conn, _, _) = _garmin_run(db)
    _write(db, user.id, conn.id, [(START - timedelta(minutes=10), 70)], route=SampleRoute.GARMIN_DAILIES)

    report = audit_module.audit(db, *WINDOW, user_id=user.id)

    assert [s["feeds"] for s in report.shared_series] == [["garmin.activity_details", "garmin.dailies"]]
    assert report.violations == 0
