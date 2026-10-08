"""A workout's own heart rate is the only heart_rate inside it.

Garmin's all-day heart rate (dailies' 15 s samples, the epoch mean, the health
snapshot) carries no device, so it resolves to the same data source as an
activity's per-second samples and shares their upsert key. Before this, whichever
arrived last owned every second both had: a run read back with a one-minute average
on each :00/:15/:30/:45 second, and an all-day value wherever the run had paused.
These tests run the webhook batch path against a real database, in both arrival
orders, because the bug only exists in what the database ends up holding.
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, DataSource
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import SeriesType, get_series_type_id
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth
from app.utils.connection_context import active_connection
from tests.factories import UserConnectionFactory, UserFactory

DAY_START = 1791259200  # a local midnight in UTC seconds: divisible by 900, as every one is
RUN_START = DAY_START + 10_747  # off the 15 s grid; the run crosses the :00 epoch boundary at +10_800
RUN_SECONDS = 180
PAUSE = range(RUN_START + 60, RUN_START + 75)  # auto-pause: the watch records nothing here
ALL_DAY_BPM = 99  # no activity sample below uses this value


def _activity_bpm(t: int) -> int:
    return 130 + (t - RUN_START) % 20


def _activity_details(*, with_hr: bool = True) -> dict[str, Any]:
    samples = []
    for t in range(RUN_START, RUN_START + RUN_SECONDS + 1):
        if t in PAUSE:
            continue
        sample: dict[str, Any] = {"startTimeInSeconds": t, "speedMetersPerSecond": 3.1}
        if with_hr:
            sample["heartRate"] = _activity_bpm(t)
        samples.append(sample)
    return {
        "summaryId": "act-1-detail",
        "activityId": 24621527012,
        "summary": {
            "activityId": 24621527012,
            "activityType": "RUNNING",
            "deviceName": "fenix 9 Pro",
            "startTimeInSeconds": RUN_START,
            "startTimeOffsetInSeconds": -14_400,
            "durationInSeconds": RUN_SECONDS,
            "averageHeartRateInBeatsPerMinute": 139 if with_hr else None,
        },
        "samples": samples,
    }


def _dailies() -> dict[str, Any]:
    # Every 15 s from five minutes before the run to five minutes after it.
    first, last = RUN_START - 300, RUN_START + RUN_SECONDS + 300
    offsets = range((first - DAY_START) // 15 * 15, last - DAY_START, 15)
    return {
        "summaryId": "daily-1",
        "calendarDate": "2026-10-05",
        "startTimeInSeconds": DAY_START,
        "startTimeOffsetInSeconds": -14_400,
        "durationInSeconds": 86_400,
        "timeOffsetHeartRateSamples": {str(o): ALL_DAY_BPM for o in offsets},
    }


def _epoch_at(slot_start: int) -> dict[str, Any]:
    return {
        "summaryId": f"x1-{slot_start:x}-RUNNING",
        "startTimeInSeconds": slot_start,
        "startTimeOffsetInSeconds": -14_400,
        "durationInSeconds": 900,
        "activityType": "RUNNING",
        "meanHeartRateInBeatsPerMinute": ALL_DAY_BPM,
    }


def _hr_rows(db: Session, user_id: UUID) -> dict[int, float]:
    rows = db.execute(
        select(DataPointSeries.recorded_at, DataPointSeries.value)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(
            DataSource.user_id == user_id,
            DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
        )
    ).all()
    return {int(r.recorded_at.timestamp()): float(r.value) for r in rows}


def _in_run(t: int) -> bool:
    return RUN_START <= t <= RUN_START + RUN_SECONDS


class TestAllDayHeartRateInsideWorkouts:
    @pytest.fixture
    def garmin_247(self) -> Garmin247Data:
        oauth = GarminOAuth(
            user_repo=MagicMock(),
            connection_repo=UserConnectionRepository(),
            provider_name="garmin",
            api_base_url="https://apis.garmin.com",
        )
        return Garmin247Data(provider_name="garmin", api_base_url="https://apis.garmin.com", oauth=oauth)

    @pytest.fixture(autouse=True)
    def _ingest_samples(self) -> Any:
        with patch.object(settings, "ingest_workout_samples", True):
            yield

    def _push(
        self, garmin_247: Garmin247Data, db: Session, connection_id: UUID, user_id: UUID, kind: str, item: dict
    ) -> None:
        with active_connection(connection_id):
            garmin_247.process_items_batch(db, user_id, kind, [item])

    def _assert_run_is_the_activity_alone(self, rows: dict[int, float]) -> None:
        in_run = {t: v for t, v in rows.items() if _in_run(t)}
        expected = {t: float(_activity_bpm(t)) for t in range(RUN_START, RUN_START + RUN_SECONDS + 1) if t not in PAUSE}
        assert in_run == expected

    def test_dailies_after_the_activity_leave_its_seconds_alone(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        conn = UserConnectionFactory(user=user, provider="garmin")

        self._push(garmin_247, db, conn.id, user.id, "activityDetails", _activity_details())
        self._push(garmin_247, db, conn.id, user.id, "dailies", _dailies())

        rows = _hr_rows(db, user.id)
        self._assert_run_is_the_activity_alone(rows)
        # Outside the run the all-day feed is the only heart rate there is, and stays.
        outside = [v for t, v in rows.items() if not _in_run(t)]
        assert outside
        assert all(v == ALL_DAY_BPM for v in outside)

    def test_an_activity_after_the_dailies_clears_them_out_of_its_span(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        conn = UserConnectionFactory(user=user, provider="garmin")

        self._push(garmin_247, db, conn.id, user.id, "dailies", _dailies())
        assert any(_in_run(t) and v == ALL_DAY_BPM for t, v in _hr_rows(db, user.id).items())

        self._push(garmin_247, db, conn.id, user.id, "activityDetails", _activity_details())

        rows = _hr_rows(db, user.id)
        # Including the grid second inside the pause, which no activity sample overwrote.
        assert any(t % 15 == 0 for t in PAUSE)
        self._assert_run_is_the_activity_alone(rows)

    def test_a_dailies_resend_after_both_changes_nothing(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        conn = UserConnectionFactory(user=user, provider="garmin")

        self._push(garmin_247, db, conn.id, user.id, "dailies", _dailies())
        self._push(garmin_247, db, conn.id, user.id, "activityDetails", _activity_details())
        self._push(garmin_247, db, conn.id, user.id, "dailies", _dailies())

        self._assert_run_is_the_activity_alone(_hr_rows(db, user.id))

    def test_an_epoch_mean_does_not_land_inside_the_run(self, garmin_247: Garmin247Data, db: Session) -> None:
        user = UserFactory()
        conn = UserConnectionFactory(user=user, provider="garmin")
        slot = DAY_START + 10_800  # a 15-minute boundary the run is recording through
        assert _in_run(slot)
        assert slot not in PAUSE

        self._push(garmin_247, db, conn.id, user.id, "activityDetails", _activity_details())
        self._push(garmin_247, db, conn.id, user.id, "epochs", _epoch_at(slot))

        self._assert_run_is_the_activity_alone(_hr_rows(db, user.id))

    def test_a_workout_with_no_heart_rate_of_its_own_keeps_the_all_day_feed(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        conn = UserConnectionFactory(user=user, provider="garmin")

        self._push(garmin_247, db, conn.id, user.id, "activityDetails", _activity_details(with_hr=False))
        self._push(garmin_247, db, conn.id, user.id, "dailies", _dailies())

        in_run = [v for t, v in _hr_rows(db, user.id).items() if _in_run(t)]
        assert in_run
        assert all(v == ALL_DAY_BPM for v in in_run)

    def test_another_accounts_workout_does_not_thin_this_accounts_feed(
        self, garmin_247: Garmin247Data, db: Session
    ) -> None:
        user = UserFactory()
        watch = UserConnectionFactory(user=user, provider="garmin")
        band = UserConnectionFactory(user=user, provider="garmin")

        self._push(garmin_247, db, watch.id, user.id, "activityDetails", _activity_details())
        self._push(garmin_247, db, band.id, user.id, "dailies", _dailies())

        band_rows = db.execute(
            select(DataPointSeries.recorded_at)
            .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
            .where(
                DataSource.user_connection_id == band.id,
                DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
            )
        ).all()
        assert any(_in_run(int(r.recorded_at.timestamp())) for r in band_rows)

    def test_activity_span_covers_summary_and_samples(self, garmin_247: Garmin247Data) -> None:
        item = _activity_details()
        samples = garmin_247._build_activity_samples(uuid4(), item)
        span = Garmin247Data._activity_span(item, samples)
        assert span == (
            datetime.fromtimestamp(RUN_START, tz=timezone.utc),
            datetime.fromtimestamp(RUN_START + RUN_SECONDS, tz=timezone.utc),
        )
