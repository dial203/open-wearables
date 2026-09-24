"""Nightly Recharge's 5-minute RMSSD series becomes timeseries samples a client can align.

AccessLink v3 states `hrv_samples` keyed "HH:MM" on a wall clock with no offset. The
night's sleep record carries the offset, so each night is placed on its own sleep start;
a night with nothing to place it on is skipped rather than guessed.
"""

from datetime import datetime
from uuid import uuid4

import pytest

from app.schemas.enums import SeriesType
from app.services.providers.polar.data_247 import Polar247Data
from app.services.providers.polar.strategy import PolarStrategy


@pytest.fixture
def data_247() -> Polar247Data:
    return PolarStrategy().data_247


def _recharge(hrv_samples: dict[str, int], date: str = "2026-09-22") -> dict:
    return {
        "polar_user": "https://www.polaraccesslink.com/v3/users/1",
        "date": date,
        "nightly_recharge_status": 4,
        "heart_rate_variability_avg": 60,
        "hrv_samples": hrv_samples,
    }


class TestRechargeHrvSeries:
    def test_each_window_lands_at_its_own_start_in_the_nights_zone(self, data_247: Polar247Data) -> None:
        starts = {"2026-09-22": datetime.fromisoformat("2026-09-21T23:31:00-04:00")}

        samples = data_247.normalize_nightly_recharge_hrv(
            [_recharge({"23:40": 55, "23:45": 61, "00:05": 70})], starts, uuid4()
        )

        assert [(s.recorded_at.isoformat(), s.value) for s in samples] == [
            ("2026-09-21T23:40:00-04:00", 55),
            ("2026-09-21T23:45:00-04:00", 61),
            # Past midnight the clock wraps, and the window belongs to the next day.
            ("2026-09-22T00:05:00-04:00", 70),
        ]
        assert {s.series_type for s in samples} == {SeriesType.heart_rate_variability_rmssd}
        assert {s.zone_offset for s in samples} == {"-04:00"}
        assert all(s.provider_metadata == {"interval_seconds": 300} for s in samples)

    def test_a_night_starting_before_midnight_whose_first_window_is_after_it(self, data_247: Polar247Data) -> None:
        starts = {"2026-09-22": datetime.fromisoformat("2026-09-21T23:58:00-04:00")}

        samples = data_247.normalize_nightly_recharge_hrv([_recharge({"00:03": 50, "00:08": 52})], starts, uuid4())

        assert [s.recorded_at.isoformat() for s in samples] == [
            "2026-09-22T00:03:00-04:00",
            "2026-09-22T00:08:00-04:00",
        ]

    def test_a_night_with_no_sleep_record_is_skipped_not_guessed(self, data_247: Polar247Data) -> None:
        samples = data_247.normalize_nightly_recharge_hrv([_recharge({"23:40": 55})], {}, uuid4())

        assert samples == []

    def test_zero_values_are_not_windows(self, data_247: Polar247Data) -> None:
        starts = {"2026-09-22": datetime.fromisoformat("2026-09-21T23:31:00+00:00")}

        samples = data_247.normalize_nightly_recharge_hrv([_recharge({"23:40": 0, "23:45": 61})], starts, uuid4())

        assert [s.value for s in samples] == [61]

    def test_the_sleep_records_start_is_keyed_by_night(self, data_247: Polar247Data) -> None:
        starts = data_247._sleep_starts(
            [{"date": "2026-09-22", "sleep_start_time": "2026-09-21T23:31:00-04:00"}, {"date": "2026-09-23"}],
            uuid4(),
        )

        assert starts == {"2026-09-22": datetime.fromisoformat("2026-09-21T23:31:00-04:00")}


class TestRechargeSyncWiring:
    def test_the_sync_fetches_sleep_once_and_saves_the_series(self, data_247: Polar247Data) -> None:
        from unittest.mock import MagicMock, patch

        sleep = [{"date": "2026-09-22", "sleep_start_time": "2026-09-21T23:31:00-04:00", "sleep_end_time": None}]
        with (
            patch.object(data_247, "get_sleep_data", return_value=sleep) as get_sleep,
            patch.object(data_247, "get_nightly_recharge_data", return_value=[_recharge({"23:40": 55})]),
            patch.object(data_247, "_save_timeseries", return_value=1) as save_ts,
            patch.object(data_247, "_save_scores", return_value=1),
        ):
            starts = datetime.fromisoformat("2026-09-21T00:00:00+00:00")
            ends = datetime.fromisoformat("2026-09-23T00:00:00+00:00")
            items = lambda: data_247.get_sleep_data(MagicMock(), uuid4(), starts, ends)  # noqa: E731
            data_247._save_nightly_recharge(MagicMock(), uuid4(), starts, ends, items)

        assert get_sleep.call_count == 1
        (saved,) = save_ts.call_args.args[1]
        assert saved.recorded_at.isoformat() == "2026-09-21T23:40:00-04:00"
