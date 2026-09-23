"""Oura's per-sleep HRV series reaches /timeseries as 5-minute windows a client can read.

A consumer re-windowing a reference recording onto these samples (the Sleep Validation
Hub aligns them against a chest strap) needs three things this file pins: the request
it builds must not be rejected, every 5-minute value must come back at its own start
time, and each sample must say how long its window is.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.services.providers.oura.strategy import OuraStrategy

SLEEP_START = "2026-09-22T00:30:00+00:00"


def _oura_sleep() -> dict:
    return {
        "id": "sleep-hrv-series",
        "day": "2026-09-22",
        "type": "long_sleep",
        "bedtime_start": SLEEP_START,
        "bedtime_end": "2026-09-22T07:30:00+00:00",
        "total_sleep_duration": 24000,
        "time_in_bed": 25200,
        "average_hrv": 70,
        "hrv": {"interval": 300, "items": [61.0, None, 74.5, 80.0], "timestamp": SLEEP_START},
        "heart_rate": {"interval": 300, "items": [58.0, 57.0, None, 55.0], "timestamp": SLEEP_START},
    }


def _save(db: Session, user: User) -> None:
    data_247 = OuraStrategy().data_247
    normalized = data_247.normalize_sleeps([_oura_sleep()], user.id)
    data_247.save_sleep_data(db, user.id, normalized)


def _get(client: TestClient, user: User, headers: dict[str, str], types: str) -> list[dict]:
    response = client.get(
        f"/api/v1/users/{user.id}/timeseries?types={types}"
        "&start_time=2026-09-22T00:00:00Z&end_time=2026-09-22T08:00:00Z&resolution=raw&limit=100",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


class TestOuraSleepHrvSeries:
    def test_each_5_minute_window_comes_back_at_its_own_start(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        _save(db, user)

        rows = _get(client, user, api_key_header, "heart_rate_variability_rmssd")

        assert [(r["timestamp"][:19], r["value"]) for r in rows] == [
            ("2026-09-22T00:30:00", 61.0),
            # The null item is a window Oura did not score: no row, not a zero.
            ("2026-09-22T00:40:00", 74.5),
            ("2026-09-22T00:45:00", 80.0),
        ]

    def test_each_sample_states_its_window_length(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        _save(db, user)

        rows = _get(client, user, api_key_header, "heart_rate_variability_rmssd")

        assert rows
        assert {r["interval_seconds"] for r in rows} == {300}

    def test_a_comma_joined_types_list_is_read_like_the_repeated_form(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        _save(db, user)

        joined = _get(
            client, user, api_key_header, "heart_rate_variability_sdnn,heart_rate_variability_rmssd,heart_rate"
        )
        repeated = _get(
            client,
            user,
            api_key_header,
            "heart_rate_variability_sdnn&types=heart_rate_variability_rmssd&types=heart_rate",
        )

        assert joined == repeated
        assert {r["type"] for r in joined} == {"heart_rate_variability_rmssd", "heart_rate"}

    def test_an_unknown_type_in_a_joined_list_is_still_rejected(
        self, client: TestClient, user: User, api_key_header: dict[str, str]
    ) -> None:
        response = client.get(
            f"/api/v1/users/{user.id}/timeseries?types=heart_rate_variability_rmssd,not_a_type"
            "&start_time=2026-09-22T00:00:00Z&end_time=2026-09-22T08:00:00Z",
            headers=api_key_header,
        )

        # The app reports every request-validation failure as 400.
        assert response.status_code == 400

    def test_a_sample_with_no_stated_window_says_so(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        from tests.factories import DataPointSeriesFactory, DataSourceFactory, SeriesTypeDefinitionFactory

        source = DataSourceFactory(user=user, provider="apple", source=f"watch-{uuid4()}")
        DataPointSeriesFactory(
            data_source=source,
            series_type=SeriesTypeDefinitionFactory.get_or_create_heart_rate(),
            recorded_at="2026-09-22T01:00:00+00:00",
            value=60,
        )
        db.commit()

        rows = _get(client, user, api_key_header, "heart_rate")

        assert [r["interval_seconds"] for r in rows] == [None]
