"""Tests for RR-interval (beat-to-beat) ingestion from Polar exercise samples.

AccessLink exposes RR as exercise sample type 11, present only when the session was
recorded with a chest strap (H6/H7/H9/H10). The API carries no per-beat clock, so these
tests pin down how the timeline is reconstructed and how missing beats are handled.
"""

from collections.abc import Iterator
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models import EventRecord, User
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.enums import SeriesType
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.services.providers.polar.oauth import PolarOAuth
from app.services.providers.polar.workouts import PolarWorkouts


@pytest.fixture(autouse=True)
def _no_v4() -> Iterator[MagicMock]:
    """These cover the v3 reconstruction, which only runs when v4 has nothing to offer."""
    with patch("app.services.providers.polar.workouts.polar_v4_data.rr_rows_for_session", return_value=None) as stub:
        yield stub


@pytest.fixture
def workouts() -> PolarWorkouts:
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


def _exercise(samples: list[dict] | None, duration: str = "PT10S") -> PolarExerciseJSON:
    return PolarExerciseJSON(
        id="2AC312F",
        device="Polar H10",
        sport="OTHER",
        start_time="2024-01-15T22:00:00",
        start_time_utc_offset=0,
        duration=duration,
        samples=samples,
    )


class TestParseRrSampleData:
    def test_parses_comma_separated_intervals(self, workouts: PolarWorkouts) -> None:
        assert workouts._parse_rr_sample_data("1000,1010,990") == [1000, 1010, 990]

    def test_empty_and_null_fields_become_missing_beats(self, workouts: PolarWorkouts) -> None:
        assert workouts._parse_rr_sample_data("1000,,null,990") == [1000, None, None, 990]

    def test_non_positive_and_unparseable_values_are_missing(self, workouts: PolarWorkouts) -> None:
        assert workouts._parse_rr_sample_data("1000,0,-5,abc") == [1000, None, None, None]


class TestBuildRrSamples:
    def test_returns_nothing_without_an_rr_sample_array(self, workouts: PolarWorkouts) -> None:
        exercise = _exercise([{"recording-rate": 5, "sample-type": "0", "data": "100,102,97"}])

        assert workouts._rr_samples_for(MagicMock(), exercise, uuid4()) == []

    def test_returns_nothing_when_samples_are_absent(self, workouts: PolarWorkouts) -> None:
        assert workouts._rr_samples_for(MagicMock(), _exercise(None), uuid4()) == []

    def test_each_beat_is_timestamped_at_the_r_wave_closing_its_interval(self, workouts: PolarWorkouts) -> None:
        exercise = _exercise([{"recording-rate": 0, "sample-type": "11", "data": "1000,900,1100"}])

        samples = workouts._rr_samples_for(MagicMock(), exercise, uuid4())

        assert [int(s.value) for s in samples] == [1000, 900, 1100]
        assert [s.recorded_at for s in samples] == [
            datetime(2024, 1, 15, 22, 0, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 22, 0, 1, 900000, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 22, 0, 3, tzinfo=timezone.utc),
        ]
        assert all(s.series_type == SeriesType.rr_interval for s in samples)
        assert all(s.device_model == "Polar H10" for s in samples)

    def test_missing_beats_absorb_the_unaccounted_time(self, workouts: PolarWorkouts) -> None:
        """Two missing beats share the 2 s the valid intervals don't account for."""
        exercise = _exercise(
            [{"recording-rate": 0, "sample-type": "11", "data": "1000,,1000,,1000"}],
            duration="PT5S",
        )

        samples = workouts._rr_samples_for(MagicMock(), exercise, uuid4())

        # Only real beats are stored, but the clock still reaches the end of the session.
        assert [int(s.value) for s in samples] == [1000, 1000, 1000]
        assert samples[-1].recorded_at == datetime(2024, 1, 15, 22, 0, 5, tzinfo=timezone.utc)

    def test_median_fills_the_gap_when_duration_leaves_no_deficit(self, workouts: PolarWorkouts) -> None:
        """A paused session can be shorter than its beats; fall back to the median interval."""
        exercise = _exercise(
            [{"recording-rate": 0, "sample-type": "11", "data": "1000,,1000"}],
            duration="PT1S",
        )

        samples = workouts._rr_samples_for(MagicMock(), exercise, uuid4())

        assert [s.recorded_at for s in samples] == [
            datetime(2024, 1, 15, 22, 0, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 15, 22, 0, 3, tzinfo=timezone.utc),
        ]

    @pytest.mark.parametrize(
        ("local_start", "offset_minutes", "zone", "first_beat_utc"),
        [
            # Polar's start time is local; UTC is local minus the offset.
            ("2024-01-15T22:00:00", 120, "+02:00", datetime(2024, 1, 15, 20, 0, 1, tzinfo=timezone.utc)),
            # An overnight H10 session in US Eastern daylight time lands after midnight UTC,
            # not on the previous afternoon as it did when the offset was added.
            ("2026-09-22T23:00:00", -240, "-04:00", datetime(2026, 9, 23, 3, 0, 1, tzinfo=timezone.utc)),
        ],
    )
    def test_the_beat_clock_starts_at_the_utc_start(
        self,
        workouts: PolarWorkouts,
        local_start: str,
        offset_minutes: int,
        zone: str,
        first_beat_utc: datetime,
    ) -> None:
        exercise = PolarExerciseJSON(
            id="2AC312F",
            device="Polar H10",
            sport="OTHER",
            start_time=local_start,
            start_time_utc_offset=offset_minutes,
            duration="PT2S",
            samples=[{"recording-rate": 0, "sample-type": "11", "data": "1000"}],
        )

        samples = workouts._rr_samples_for(MagicMock(), exercise, uuid4())

        assert samples[0].recorded_at == first_beat_utc
        assert samples[0].zone_offset == zone

    def test_v4_is_matched_on_the_local_start(self, workouts: PolarWorkouts) -> None:
        """v4 lists sessions in local time, so the lookup must not be given the UTC start."""
        exercise = PolarExerciseJSON(
            id="2AC312F",
            device="Polar H10",
            sport="OTHER",
            start_time="2026-09-22T23:00:00",
            start_time_utc_offset=-240,
            duration="PT2S",
        )

        with patch(
            "app.services.providers.polar.workouts.polar_v4_data.rr_rows_for_session", return_value=None
        ) as lookup:
            workouts._rr_samples_for(MagicMock(), exercise, uuid4())

        assert lookup.call_args.args[2] == datetime(2026, 9, 22, 23, 0)


class TestSamplesAreRequested:
    def test_exercise_list_asks_for_samples_by_default(self, workouts: PolarWorkouts) -> None:
        with patch.object(workouts, "_make_api_request", return_value=[]) as request:
            workouts.get_workouts_from_api(MagicMock(), uuid4())

        assert request.call_args.kwargs["params"]["samples"] == "true"

    def test_webhook_fetch_asks_for_samples(self, workouts: PolarWorkouts) -> None:
        with patch.object(workouts, "_make_api_request", return_value=None) as request:
            workouts.fetch_and_save_exercise(MagicMock(), uuid4(), "/v3/exercises/2AC312F")

        assert request.call_args.kwargs["params"] == {"samples": "true"}
