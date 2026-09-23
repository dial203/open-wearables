"""Tests for the AccessLink v4 reads and the v4-first RR hybrid.

v4 exists here for one reason: its RR beats each carry their own duration, so the beat
clock is exact where v3's has to be estimated across dropouts. These tests pin down that
v4 wins when it has data, that v3 still runs when it doesn't, and that optical PPI never
gets filed as ECG RR.
"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import User
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.services.providers.polar.v4_data import PolarV4Data
from app.services.providers.polar.workouts import PolarWorkouts
from tests.factories import DataSourceFactory, EventRecordFactory, UserFactory

SESSION = {
    "trainingSessions": [
        {
            "startTime": "2024-01-15T22:00:00",
            "timezoneOffsetMinutes": 0,
            "exercises": [
                {
                    "startTime": "2024-01-15T22:00:00",
                    "samples": {
                        "rrSamples": [
                            {"durationMillis": 1000, "offline": False},
                            {"durationMillis": 850, "offline": True},
                            {"durationMillis": 900, "offline": False},
                        ]
                    },
                }
            ],
        }
    ]
}


@pytest.fixture
def v4() -> PolarV4Data:
    return PolarV4Data()


class TestRrRowsForSession:
    def test_returns_none_without_a_v4_connection(self, v4: PolarV4Data) -> None:
        with patch.object(v4, "is_connected", return_value=False):
            assert v4.rr_rows_for_session(MagicMock(), uuid4(), datetime(2024, 1, 15, 22)) is None

    def test_beats_keep_their_own_duration_and_offline_flag(self, v4: PolarV4Data) -> None:
        with patch.object(v4, "is_connected", return_value=True), patch.object(v4, "_get", return_value=SESSION):
            rows = v4.rr_rows_for_session(MagicMock(), uuid4(), datetime(2024, 1, 15, 22))

        assert rows == [(1000, False), (850, True), (900, False)]

    def test_returns_none_when_no_session_starts_near_that_time(self, v4: PolarV4Data) -> None:
        with patch.object(v4, "is_connected", return_value=True), patch.object(v4, "_get", return_value=SESSION):
            rows = v4.rr_rows_for_session(MagicMock(), uuid4(), datetime(2024, 1, 15, 20))

        assert rows is None

    def test_a_session_without_rr_samples_falls_through(self, v4: PolarV4Data) -> None:
        strapless = {"trainingSessions": [{"startTime": "2024-01-15T22:00:00", "exercises": [{"samples": {}}]}]}
        with patch.object(v4, "is_connected", return_value=True), patch.object(v4, "_get", return_value=strapless):
            assert v4.rr_rows_for_session(MagicMock(), uuid4(), datetime(2024, 1, 15, 22)) is None

    def test_features_request_is_scoped_to_a_single_day(self, v4: PolarV4Data) -> None:
        """v4 caps the range at one day whenever a feature is asked for."""
        with patch.object(v4, "is_connected", return_value=True), patch.object(v4, "_get", return_value=SESSION) as get:
            v4.rr_rows_for_session(MagicMock(), uuid4(), datetime(2024, 1, 15, 22))

        params = get.call_args.args[3]
        assert params == {"from": "2024-01-15", "to": "2024-01-16", "features": "samples"}

    def test_day_cache_collapses_repeat_fetches_within_one_pull(self, v4: PolarV4Data) -> None:
        """A pull re-walks every exercise Flow lists; without the cache each one re-asks v4."""
        cache: dict = {}
        with patch.object(v4, "is_connected", return_value=True), patch.object(v4, "_get", return_value=SESSION) as get:
            for _ in range(3):
                v4.rr_rows_for_session(MagicMock(), uuid4(), datetime(2024, 1, 15, 22), cache)

        assert get.call_count == 1


def _ppi_day(day: str, *beats: dict) -> dict:
    return {"dailyPpiSamples": [{"date": day, "ppiSamplesPerDevice": [{"ppiSamples": list(beats)}]}]}


UTC_DAY = {date(2024, 1, 15): 0}


class TestNormalizePpi:
    def test_beats_are_timestamped_from_local_midnight_at_utc_plus_0(self, v4: PolarV4Data) -> None:
        raw = _ppi_day(
            "2024-01-15",
            {"offsetMillis": 1000, "ppInterval": 900, "skinContact": True, "offline": False},
            {"offsetMillis": 2000, "ppInterval": 950, "skinContact": True, "offline": False},
        )

        samples = v4.normalize_ppi(raw, uuid4(), UTC_DAY)

        assert [s.recorded_at for s in samples] == [
            datetime(2024, 1, 15, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 0, 0, 2, tzinfo=timezone.utc),
        ]
        assert [int(s.value) for s in samples] == [900, 950]

    def test_edt_beats_land_at_true_utc(self, v4: PolarV4Data) -> None:
        """offsetMillis counts from *local* midnight; in EDT that midnight is 04:00Z.

        Stored naively, 23:30 local on the 15th sat at 23:30Z on the 15th — 4 h early and
        outside the H10 session it has to be compared against.
        """
        half_past_eleven = (23 * 3600 + 30 * 60) * 1000
        raw = _ppi_day(
            "2024-07-15",
            {"offsetMillis": 0, "ppInterval": 900, "skinContact": True},
            {"offsetMillis": half_past_eleven, "ppInterval": 950, "skinContact": True},
        )

        samples = v4.normalize_ppi(raw, uuid4(), {date(2024, 7, 15): -240})

        assert [s.recorded_at for s in samples] == [
            datetime(2024, 7, 15, 4, 0, tzinfo=timezone.utc),
            datetime(2024, 7, 16, 3, 30, tzinfo=timezone.utc),
        ]
        assert {s.zone_offset for s in samples} == {"-04:00"}

    def test_east_of_utc_the_day_starts_the_evening_before(self, v4: PolarV4Data) -> None:
        raw = _ppi_day("2024-01-15", {"offsetMillis": 0, "ppInterval": 900, "skinContact": True})

        sample = v4.normalize_ppi(raw, uuid4(), {date(2024, 1, 15): 120})[0]

        assert sample.recorded_at == datetime(2024, 1, 14, 22, 0, tzinfo=timezone.utc)
        assert sample.zone_offset == "+02:00"

    def test_a_day_without_a_known_offset_is_skipped_not_guessed(self, v4: PolarV4Data) -> None:
        raw = _ppi_day("2024-01-15", {"offsetMillis": 0, "ppInterval": 900, "skinContact": True})

        assert v4.normalize_ppi(raw, uuid4(), {}) == []

    def test_ppi_is_never_stored_as_ecg_rr(self, v4: PolarV4Data) -> None:
        raw = _ppi_day("2024-01-15", {"offsetMillis": 0, "ppInterval": 900, "skinContact": True})

        samples = v4.normalize_ppi(raw, uuid4(), UTC_DAY)

        assert all(s.series_type == SeriesType.pulse_to_pulse_interval for s in samples)
        assert not any(s.series_type == SeriesType.rr_interval for s in samples)

    def test_beats_off_the_wrist_or_offline_are_dropped(self, v4: PolarV4Data) -> None:
        raw = _ppi_day(
            "2024-01-15",
            {"offsetMillis": 0, "ppInterval": 900, "skinContact": False, "offline": False},
            {"offsetMillis": 1, "ppInterval": 900, "skinContact": True, "offline": True},
            {"offsetMillis": 2, "ppInterval": 900, "skinContact": True, "offline": False},
        )

        assert len(v4.normalize_ppi(raw, uuid4(), UTC_DAY)) == 1

    def test_movement_beats_are_kept(self, v4: PolarV4Data) -> None:
        """Motion artefact is part of what an optical comparator does; dropping it would flatter it."""
        raw = _ppi_day("2024-01-15", {"offsetMillis": 0, "ppInterval": 900, "skinContact": True, "movement": True})

        assert len(v4.normalize_ppi(raw, uuid4(), UTC_DAY)) == 1

    def test_ppi_lands_on_the_ordinary_polar_source(self, v4: PolarV4Data) -> None:
        raw = _ppi_day("2024-01-15", {"offsetMillis": 0, "ppInterval": 900, "skinContact": True})

        sample = v4.normalize_ppi(raw, uuid4(), UTC_DAY)[0]

        assert sample.provider == ProviderName.POLAR
        assert sample.source == ProviderName.POLAR


class TestPpiUtcOffsets:
    """Where a PPI day's offset comes from, since /ppi-samples carries none."""

    @staticmethod
    def _polar_record(
        db: Session,
        user: User,
        start: datetime,
        zone_offset: str | None,
        provider: ProviderName = ProviderName.POLAR,
    ) -> None:
        source = DataSourceFactory(user=user, provider=provider, source=provider.value)
        EventRecordFactory(data_source=source, start_datetime=start, zone_offset=zone_offset)
        db.flush()

    def test_a_nearby_polar_exercise_lends_its_offset(self, db: Session, v4: PolarV4Data) -> None:
        user = UserFactory()
        self._polar_record(db, user, datetime(2024, 7, 16, 3, 0, tzinfo=timezone.utc), "-04:00")

        with patch.object(v4, "_get") as get:
            offsets = v4.ppi_utc_offsets(db, user.id, date(2024, 7, 15), date(2024, 7, 16))

        assert offsets == {date(2024, 7, 15): -240, date(2024, 7, 16): -240}
        get.assert_not_called()

    def test_the_record_nearest_local_midnight_wins_across_a_dst_change(self, db: Session, v4: PolarV4Data) -> None:
        """US DST ended 2026-11-01 02:00. offsetMillis counts from midnight, so that day is EDT."""
        user = UserFactory()
        self._polar_record(db, user, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc), "-04:00")  # 20:00 EDT, 31 Oct
        self._polar_record(db, user, datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc), "-05:00")  # 22:00 EST, 1 Nov

        offsets = v4.ppi_utc_offsets(db, user.id, date(2026, 11, 1), date(2026, 11, 2))

        assert offsets == {date(2026, 11, 1): -240, date(2026, 11, 2): -300}

    def test_other_providers_and_other_users_do_not_count(self, db: Session, v4: PolarV4Data) -> None:
        user = UserFactory()
        start = datetime(2024, 7, 15, 12, 0, tzinfo=timezone.utc)
        self._polar_record(db, user, start, "+09:00", provider=ProviderName.GARMIN)
        self._polar_record(db, UserFactory(), start, "+02:00")

        with patch.object(v4, "_get", return_value=None):
            assert v4.ppi_utc_offsets(db, user.id, date(2024, 7, 15), date(2024, 7, 15)) == {}

    def test_a_record_outside_the_window_does_not_count(self, db: Session, v4: PolarV4Data) -> None:
        user = UserFactory()
        self._polar_record(db, user, datetime(2024, 7, 1, 12, 0, tzinfo=timezone.utc), "-04:00")

        with patch.object(v4, "_get", return_value=None):
            assert v4.ppi_utc_offsets(db, user.id, date(2024, 7, 15), date(2024, 7, 15)) == {}

    def test_falls_back_to_the_account_timezone_once(self, db: Session, v4: PolarV4Data) -> None:
        user = UserFactory()
        account = {"accountData": {"localizationSettings": {"timezoneOffsetMinutes": -240}}}

        with patch.object(v4, "_get", return_value=account) as get:
            offsets = v4.ppi_utc_offsets(db, user.id, date(2024, 7, 15), date(2024, 7, 17))

        assert offsets == {date(2024, 7, 15): -240, date(2024, 7, 16): -240, date(2024, 7, 17): -240}
        get.assert_called_once()
        assert get.call_args.args[2] == "/v4/data/user/account-data"


class TestPpiLinesUpWithH10:
    """The point of the fix: an EDT night's optical beats sit on the same UTC clock as the H10's."""

    def test_same_beat_same_instant(self, v4: PolarV4Data) -> None:
        exercise = PolarExerciseJSON(
            id="2AC312F",
            device="Polar H10",
            sport="OTHER",
            start_time="2024-07-15T23:00:00",
            start_time_utc_offset=-240,
            duration="PT3S",
            samples=[{"recording-rate": 0, "sample-type": "11", "data": "1000,900"}],
        )
        with patch("app.services.providers.polar.workouts.polar_v4_data.rr_rows_for_session", return_value=None):
            rr = TestHybridPreference._workouts()._rr_samples_for(MagicMock(), exercise, uuid4())

        # The watch sees the same R-wave-closing beat 23:00:01 local on the 15th.
        raw = _ppi_day("2024-07-15", {"offsetMillis": (23 * 3600 + 1) * 1000, "ppInterval": 1000, "skinContact": True})
        ppi = v4.normalize_ppi(raw, uuid4(), {date(2024, 7, 15): -240})

        assert rr[0].recorded_at == datetime(2024, 7, 16, 3, 0, 1, tzinfo=timezone.utc)
        assert ppi[0].recorded_at == rr[0].recorded_at


class TestHybridPreference:
    """The v3 reconstruction must not run when v4 has the same beats exactly."""

    @staticmethod
    def _workouts() -> "PolarWorkouts":
        from app.models import EventRecord, User
        from app.repositories.event_record_repository import EventRecordRepository
        from app.repositories.user_connection_repository import UserConnectionRepository
        from app.repositories.user_repository import UserRepository
        from app.services.providers.polar.oauth import PolarOAuth

        oauth = PolarOAuth(
            user_repo=UserRepository(User),
            connection_repo=UserConnectionRepository(),
            provider_name="polar",
            api_base_url="https://www.polaraccesslink.com",
        )
        return PolarWorkouts(
            workout_repo=EventRecordRepository(EventRecord),
            connection_repo=UserConnectionRepository(),
            provider_name="polar",
            api_base_url="https://www.polaraccesslink.com",
            oauth=oauth,
        )

    @staticmethod
    def _exercise() -> PolarExerciseJSON:
        return PolarExerciseJSON(
            id="2AC312F",
            device="Polar H10",
            sport="OTHER",
            start_time="2024-01-15T22:00:00",
            start_time_utc_offset=0,
            duration="PT3S",
            samples=[{"recording-rate": 0, "sample-type": "11", "data": "1000,,900"}],
        )

    def test_v4_rows_win_over_the_v3_reconstruction(self) -> None:
        workouts = self._workouts()
        rows = [(1000, False), (850, True), (900, False)]

        with patch("app.services.providers.polar.workouts.polar_v4_data.rr_rows_for_session", return_value=rows):
            samples = workouts._rr_samples_for(MagicMock(), self._exercise(), uuid4())

        # The offline beat still advances the clock but is not stored, so the third beat
        # sits at 1000 + 850 + 900 ms — a timestamp the v3 path could only estimate.
        assert [int(s.value) for s in samples] == [1000, 900]
        assert samples[-1].recorded_at == datetime(2024, 1, 15, 22, 0, 2, 750000, tzinfo=timezone.utc)

    def test_v3_still_runs_when_v4_has_nothing(self) -> None:
        workouts = self._workouts()

        with patch("app.services.providers.polar.workouts.polar_v4_data.rr_rows_for_session", return_value=None):
            samples = workouts._rr_samples_for(MagicMock(), self._exercise(), uuid4())

        assert [int(s.value) for s in samples] == [1000, 900]
        assert all(s.series_type == SeriesType.rr_interval for s in samples)
