"""Tests for the AccessLink v4 reads and the v4-first RR hybrid.

v4 exists here for one reason: its RR beats each carry their own duration, so the beat
clock is exact where v3's has to be estimated across dropouts. These tests pin down that
v4 wins when it has data, that v3 still runs when it doesn't, and that optical PPI never
gets filed as ECG RR.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.schemas.enums import ProviderName, SeriesType
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.services.providers.polar.v4_data import PolarV4Data
from app.services.providers.polar.workouts import PolarWorkouts

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


class TestNormalizePpi:
    def test_beats_are_timestamped_from_the_start_of_the_day(self, v4: PolarV4Data) -> None:
        raw = {
            "dailyPpiSamples": [
                {
                    "date": "2024-01-15",
                    "ppiSamplesPerDevice": [
                        {
                            "ppiSamples": [
                                {"offsetMillis": 1000, "ppInterval": 900, "skinContact": True, "offline": False},
                                {"offsetMillis": 2000, "ppInterval": 950, "skinContact": True, "offline": False},
                            ]
                        }
                    ],
                }
            ]
        }

        samples = v4.normalize_ppi(raw, uuid4())

        assert [s.recorded_at for s in samples] == [
            datetime(2024, 1, 15, 0, 0, 1),
            datetime(2024, 1, 15, 0, 0, 2),
        ]
        assert [int(s.value) for s in samples] == [900, 950]

    def test_ppi_is_never_stored_as_ecg_rr(self, v4: PolarV4Data) -> None:
        raw = {
            "dailyPpiSamples": [
                {
                    "date": "2024-01-15",
                    "ppiSamplesPerDevice": [
                        {"ppiSamples": [{"offsetMillis": 0, "ppInterval": 900, "skinContact": True}]}
                    ],
                }
            ]
        }

        samples = v4.normalize_ppi(raw, uuid4())

        assert all(s.series_type == SeriesType.pulse_to_pulse_interval for s in samples)
        assert not any(s.series_type == SeriesType.rr_interval for s in samples)

    def test_beats_off_the_wrist_or_offline_are_dropped(self, v4: PolarV4Data) -> None:
        raw = {
            "dailyPpiSamples": [
                {
                    "date": "2024-01-15",
                    "ppiSamplesPerDevice": [
                        {
                            "ppiSamples": [
                                {"offsetMillis": 0, "ppInterval": 900, "skinContact": False, "offline": False},
                                {"offsetMillis": 1, "ppInterval": 900, "skinContact": True, "offline": True},
                                {"offsetMillis": 2, "ppInterval": 900, "skinContact": True, "offline": False},
                            ]
                        }
                    ],
                }
            ]
        }

        assert len(v4.normalize_ppi(raw, uuid4())) == 1

    def test_movement_beats_are_kept(self, v4: PolarV4Data) -> None:
        """Motion artefact is part of what an optical comparator does; dropping it would flatter it."""
        raw = {
            "dailyPpiSamples": [
                {
                    "date": "2024-01-15",
                    "ppiSamplesPerDevice": [
                        {"ppiSamples": [{"offsetMillis": 0, "ppInterval": 900, "skinContact": True, "movement": True}]}
                    ],
                }
            ]
        }

        assert len(v4.normalize_ppi(raw, uuid4())) == 1

    def test_ppi_lands_on_the_ordinary_polar_source(self, v4: PolarV4Data) -> None:
        raw = {
            "dailyPpiSamples": [
                {
                    "date": "2024-01-15",
                    "ppiSamplesPerDevice": [
                        {"ppiSamples": [{"offsetMillis": 0, "ppInterval": 900, "skinContact": True}]}
                    ],
                }
            ]
        }

        sample = v4.normalize_ppi(raw, uuid4())[0]

        assert sample.provider == ProviderName.POLAR
        assert sample.source == ProviderName.POLAR


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
        assert samples[-1].recorded_at == datetime(2024, 1, 15, 22, 0, 2, 750000)

    def test_v3_still_runs_when_v4_has_nothing(self) -> None:
        workouts = self._workouts()

        with patch("app.services.providers.polar.workouts.polar_v4_data.rr_rows_for_session", return_value=None):
            samples = workouts._rr_samples_for(MagicMock(), self._exercise(), uuid4())

        assert [int(s.value) for s in samples] == [1000, 900]
        assert all(s.series_type == SeriesType.rr_interval for s in samples)
