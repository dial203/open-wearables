"""Daily aggregates count a day once when a maker is connected twice.

The record and sample endpoints already leave out an aggregator's copy of a brand that
also arrives directly. The summaries are where a second copy does the most damage: it is
not a duplicate row a person can ignore, it is a wrong number - 14,000 steps for a day
somebody walked 7,000 - and once it is in an analysis nothing about it looks wrong.

These cover the three daily summaries (activity, sleep, recovery) and, as with the rest
of the rule, what must still be counted: history from before the direct connection, a
brand with no direct route, and every copy when the read asks for them.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataSource, User
from app.schemas.enums import HealthScoreCategory
from tests.factories import (
    ApiKeyFactory,
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    SeriesTypeDefinitionFactory,
    SleepDetailsFactory,
    UserFactory,
)
from tests.utils import api_key_headers

DAY = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)
WINDOW = {"start_date": "2026-05-01T00:00:00Z", "end_date": "2026-05-08T00:00:00Z"}


def _headers() -> dict[str, str]:
    return api_key_headers(ApiKeyFactory().plain_key)


def _direct_garmin(user: User) -> DataSource:
    return DataSourceFactory(
        user=user,
        provider="garmin",
        device_model="fenix 8",
        source="garmin_api",
        original_source_name="Garmin",
    )


def _relayed_garmin(user: User, **kwargs) -> DataSource:
    """The same Garmin arriving a second time through Apple Health."""
    return DataSourceFactory(
        user=user,
        provider="apple",
        device_model=None,
        source="com.garmin.connect.mobile",
        original_source_name="Garmin",
        **kwargs,
    )


def _steps(data_source: DataSource, at: datetime, value: int) -> None:
    DataPointSeriesFactory(
        data_source=data_source,
        series_type=SeriesTypeDefinitionFactory.get_or_create_steps(),
        recorded_at=at,
        value=Decimal(value),
        is_daily_total=True,
    )


def _sleep(data_source: DataSource, start: datetime, minutes: int = 420) -> None:
    end = start + timedelta(minutes=minutes)
    record = EventRecordFactory(
        data_source=data_source,
        category="sleep",
        type="asleep",
        start_datetime=start,
        end_datetime=end,
        duration_seconds=minutes * 60,
    )
    SleepDetailsFactory(event_record=record, sleep_total_duration_minutes=minutes)


def _activity(client: TestClient, user: User, headers: dict[str, str], **extra) -> dict:
    response = client.get(
        f"/api/v1/users/{user.id}/summaries/activity",
        headers=headers,
        params={**WINDOW, "filter_by_priority": False, **extra},
    )
    assert response.status_code == 200
    return response.json()


class TestActivitySummaries:
    def test_a_day_both_routes_reported_is_counted_once(self, client: TestClient, db: Session) -> None:
        """The number, not the row, is what matters: 7,000 steps must not read as 14,000."""
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _steps(direct, DAY, 7000)
        _steps(relayed, DAY + timedelta(minutes=2), 7000)
        db.commit()

        body = _activity(client, user, _headers())

        assert [row["steps"] for row in body["data"]] == [7000]
        assert body["data"][0]["source"]["provider"] == "garmin"

    def test_what_was_left_out_is_reported(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _steps(direct, DAY, 7000)
        _steps(relayed, DAY + timedelta(minutes=2), 7000)
        db.commit()

        dedup = _activity(client, user, _headers())["metadata"]["relay_dedup"]

        assert dedup["applied"] is True
        assert [entry["data_source_id"] for entry in dedup["hidden"]] == [str(relayed.id)]
        assert dedup["hidden"][0]["direct_provider"] == "garmin"

    def test_asking_for_every_copy_counts_both(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _steps(direct, DAY, 7000)
        _steps(relayed, DAY + timedelta(minutes=2), 7000)
        db.commit()

        body = _activity(client, user, _headers(), include_redundant_relays=True)

        assert sorted(row["steps"] for row in body["data"]) == [7000, 7000]
        assert body["metadata"]["relay_dedup"] is None

    def test_a_day_only_the_relay_reported_is_still_counted(self, client: TestClient, db: Session) -> None:
        """Before the Garmin was connected, the relay is the only record of that day."""
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _steps(direct, DAY, 7000)
        _steps(relayed, DAY - timedelta(days=2), 5500)
        db.commit()

        body = _activity(client, user, _headers())

        assert sorted(row["steps"] for row in body["data"]) == [5500, 7000]

    def test_a_brand_with_no_direct_route_is_counted(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        direct = _direct_garmin(user)
        zepp = DataSourceFactory(
            user=user, provider="apple", device_model=None, source="Zepp Life", original_source_name="Zepp"
        )
        _steps(direct, DAY, 7000)
        _steps(zepp, DAY, 3000)
        db.commit()

        body = _activity(client, user, _headers())

        assert sorted(row["steps"] for row in body["data"]) == [3000, 7000]

    def test_the_default_priority_view_reports_the_direct_source(self, client: TestClient, db: Session) -> None:
        """With one row per day, it has to be the maker's own, not the relay's copy."""
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _steps(direct, DAY, 7000)
        _steps(relayed, DAY + timedelta(minutes=2), 7000)
        db.commit()

        response = client.get(
            f"/api/v1/users/{user.id}/summaries/activity",
            headers=_headers(),
            params=WINDOW,
        )

        assert response.status_code == 200
        rows = response.json()["data"]
        assert [row["source"]["provider"] for row in rows] == ["garmin"]
        assert [row["steps"] for row in rows] == [7000]

    def test_the_global_switch_turns_it_off(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "relay_dedup_enabled", False)
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _steps(direct, DAY, 7000)
        _steps(relayed, DAY + timedelta(minutes=2), 7000)
        db.commit()

        body = _activity(client, user, _headers())

        assert sorted(row["steps"] for row in body["data"]) == [7000, 7000]
        assert body["metadata"]["relay_dedup"] is None


class TestSleepSummaries:
    def test_a_night_both_routes_recorded_is_summarised_once(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _sleep(direct, DAY - timedelta(hours=8))
        _sleep(relayed, DAY - timedelta(hours=8) + timedelta(minutes=6))
        db.commit()

        response = client.get(
            f"/api/v1/users/{user.id}/summaries/sleep",
            headers=_headers(),
            params={**WINDOW, "filter_by_priority": False},
        )

        assert response.status_code == 200
        body = response.json()
        assert [row["source"]["provider"] for row in body["data"]] == ["garmin"]
        assert body["metadata"]["relay_dedup"]["applied"] is True

    def test_a_night_only_the_relay_recorded_survives(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _sleep(direct, DAY - timedelta(hours=8))
        _sleep(relayed, DAY - timedelta(days=3, hours=8))
        db.commit()

        response = client.get(
            f"/api/v1/users/{user.id}/summaries/sleep",
            headers=_headers(),
            params={**WINDOW, "filter_by_priority": False},
        )

        providers = [row["source"]["provider"] for row in response.json()["data"]]
        assert sorted(providers) == ["apple", "garmin"]


class TestRecoverySummaries:
    def test_a_day_scored_by_both_routes_is_returned_once(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        HealthScoreFactory(
            data_source=direct,
            user_id=user.id,
            category=HealthScoreCategory.RECOVERY,
            provider="garmin",
            recorded_at=DAY,
            components={"resting_heart_rate": 48},
        )
        HealthScoreFactory(
            data_source=relayed,
            user_id=user.id,
            category=HealthScoreCategory.RECOVERY,
            provider="apple",
            recorded_at=DAY + timedelta(hours=2),
            components={"resting_heart_rate": 48},
        )
        db.commit()

        response = client.get(
            f"/api/v1/users/{user.id}/summaries/recovery",
            headers=_headers(),
            params={**WINDOW, "filter_by_priority": False},
        )

        assert response.status_code == 200
        body = response.json()
        assert [row["source"]["provider"] for row in body["data"]] == ["garmin"]
        assert body["metadata"]["relay_dedup"]["applied"] is True

    def test_a_score_with_no_data_source_is_never_dropped(self, client: TestClient, db: Session) -> None:
        """Older scores carry no source; a NULL must not be read as "redundant"."""
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        HealthScoreFactory(
            data_source=direct,
            user_id=user.id,
            category=HealthScoreCategory.RECOVERY,
            provider="garmin",
            recorded_at=DAY,
        )
        HealthScoreFactory(
            data_source=relayed,
            user_id=user.id,
            category=HealthScoreCategory.RECOVERY,
            provider="apple",
            recorded_at=DAY + timedelta(hours=2),
        )
        HealthScoreFactory(
            data_source_id=None,
            user_id=user.id,
            category=HealthScoreCategory.RECOVERY,
            provider="whoop",
            recorded_at=DAY + timedelta(days=1),
        )
        db.commit()

        response = client.get(
            f"/api/v1/users/{user.id}/summaries/recovery",
            headers=_headers(),
            params={**WINDOW, "filter_by_priority": False},
        )

        providers = sorted(row["source"]["provider"] for row in response.json()["data"])
        assert providers == ["garmin", "whoop"]


class TestSeriesTypeGranularity:
    def test_a_metric_only_the_relay_carries_is_still_counted(self, client: TestClient, db: Session) -> None:
        """Coverage is measured per series type, in summaries as everywhere else."""
        user = UserFactory()
        direct, relayed = _direct_garmin(user), _relayed_garmin(user)
        _steps(direct, DAY, 7000)
        DataPointSeriesFactory(
            data_source=relayed,
            series_type=SeriesTypeDefinitionFactory.get_or_create_flights_climbed(),
            recorded_at=DAY,
            value=Decimal(12),
            is_daily_total=True,
        )
        db.commit()

        body = _activity(client, user, _headers())

        floors = [row.get("floors_climbed") for row in body["data"] if row.get("floors_climbed")]
        assert floors, "a metric the direct route never sent must survive the rule"
