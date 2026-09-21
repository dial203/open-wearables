"""The per-source preview the device registry reads to identify a device.

A source called "Bluetooth Device" or a bare bundle id names nothing. What it
reported names it: nights of sleep and a morning readiness score describe a ring or
a band, a run of workouts describes something worn to train in, and silence
describes a device that came off. These pin that the evidence is complete, scoped to
the window, and carries the account it arrived through.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataSource, User
from app.schemas.enums import HealthScoreCategory, ProviderName
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    SeriesTypeDefinitionFactory,
    UserConnectionFactory,
    UserFactory,
)

ENDPOINT = "/api/v1/users/{user_id}/devices/source-activity"


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


@pytest.fixture
def source(db: Session, user: User) -> DataSource:
    data_source = DataSourceFactory(user=user, provider=ProviderName.APPLE, source="Bluetooth Device")
    db.commit()
    return data_source


def _get(client: TestClient, user: User, headers: dict[str, str], **params: object) -> dict:
    response = client.get(ENDPOINT.format(user_id=user.id), headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _item(body: dict, data_source_id: object) -> dict:
    match = next((i for i in body["items"] if i["data_source_id"] == str(data_source_id)), None)
    assert match is not None, f"{data_source_id} missing from {[i['data_source_id'] for i in body['items']]}"
    return match


class TestWhatTheSourceReported:
    def test_sleep_and_workouts_are_counted_by_category(
        self, client: TestClient, db: Session, user: User, source: DataSource, auth_headers: dict[str, str]
    ) -> None:
        for night in range(3):
            EventRecordFactory(data_source=source, category="sleep", type_="sleep", start_datetime=_ago(night + 1))
        EventRecordFactory(data_source=source, category="workout", start_datetime=_ago(2))
        db.commit()

        item = _item(_get(client, user, auth_headers), source.id)

        assert {b["label"]: b["count"] for b in item["events"]} == {"sleep": 3, "workout": 1}
        assert item["last_seen_at"] is not None

    def test_scores_are_counted_by_category(
        self, client: TestClient, db: Session, user: User, source: DataSource, auth_headers: dict[str, str]
    ) -> None:
        """A morning score every day is what separates a ring from a phone."""
        for day in range(2):
            HealthScoreFactory(
                data_source_id=source.id,
                user_id=user.id,
                category=HealthScoreCategory.READINESS,
                recorded_at=_ago(day + 1),
            )
        db.commit()

        item = _item(_get(client, user, auth_headers), source.id)

        assert {b["label"]: b["count"] for b in item["scores"]} == {"readiness": 2}

    def test_metrics_name_the_series_the_source_carries(
        self, client: TestClient, db: Session, user: User, source: DataSource, auth_headers: dict[str, str]
    ) -> None:
        heart_rate = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        for minute in range(4):
            DataPointSeriesFactory(
                data_source=source,
                series_type=heart_rate,
                recorded_at=_ago(1) + timedelta(minutes=minute),
            )
        db.commit()

        item = _item(_get(client, user, auth_headers), source.id)

        assert {b["label"]: b["count"] for b in item["metrics"]} == {"heart_rate": 4}

    def test_a_source_that_reported_nothing_is_listed_with_no_last_seen(
        self, client: TestClient, db: Session, user: User, source: DataSource, auth_headers: dict[str, str]
    ) -> None:
        """Silence is evidence too, so the row stays rather than being filtered out."""
        item = _item(_get(client, user, auth_headers), source.id)

        assert item["last_seen_at"] is None
        assert item["events"] == []
        assert item["scores"] == []
        assert item["metrics"] == []


class TestTheWindow:
    def test_rows_outside_the_window_are_not_counted(
        self, client: TestClient, db: Session, user: User, source: DataSource, auth_headers: dict[str, str]
    ) -> None:
        EventRecordFactory(data_source=source, category="sleep", start_datetime=_ago(2))
        EventRecordFactory(data_source=source, category="sleep", start_datetime=_ago(90))
        db.commit()

        within = _item(_get(client, user, auth_headers, days=30), source.id)
        assert {b["label"]: b["count"] for b in within["events"]} == {"sleep": 1}

        wider = _item(_get(client, user, auth_headers, days=180), source.id)
        assert {b["label"]: b["count"] for b in wider["events"]} == {"sleep": 2}

    def test_the_window_is_reported_back(self, client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
        assert _get(client, user, auth_headers, days=7)["window_days"] == 7

    def test_an_out_of_range_window_is_refused_rather_than_silently_clamped(
        self, client: TestClient, user: User, auth_headers: dict[str, str]
    ) -> None:
        """These are aggregates over the largest tables; an unbounded window scans them."""
        response = client.get(ENDPOINT.format(user_id=user.id), headers=auth_headers, params={"days": 5000})
        assert response.status_code == 400


class TestTheAccountTravelsWithIt:
    def test_each_source_names_the_account_and_its_classification(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(
            user=user,
            provider="garmin",
            account_label="P01 arm A",
            account_email="p01.a@lab.example.edu",
            account_type="validation",
        )
        db.commit()
        data_source = DataSourceFactory(user=user, provider=ProviderName.GARMIN, user_connection_id=connection.id)
        db.commit()

        item = _item(_get(client, user, auth_headers), data_source.id)

        assert item["user_connection_id"] == str(connection.id)
        assert item["account_label"] == "P01 arm A"
        assert item["account_email"] == "p01.a@lab.example.edu"
        assert item["account_type"] == "validation"

    def test_a_one_time_import_names_no_account(
        self, client: TestClient, db: Session, user: User, source: DataSource, auth_headers: dict[str, str]
    ) -> None:
        item = _item(_get(client, user, auth_headers), source.id)

        assert item["user_connection_id"] is None
        assert item["account_email"] is None


class TestScoping:
    def test_another_users_sources_are_not_included(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        theirs = DataSourceFactory(user=UserFactory(), provider=ProviderName.OURA)
        EventRecordFactory(data_source=theirs, category="sleep", start_datetime=_ago(1))
        db.commit()

        body = _get(client, user, auth_headers)

        assert str(theirs.id) not in {i["data_source_id"] for i in body["items"]}

    def test_a_user_with_no_sources_returns_an_empty_list(
        self, client: TestClient, db: Session, auth_headers: dict[str, str]
    ) -> None:
        empty = UserFactory()
        db.commit()

        body = _get(client, empty, auth_headers)

        assert body == {"items": [], "total": 0, "window_days": 30}

    def test_the_route_is_not_swallowed_by_the_device_id_path(
        self, client: TestClient, user: User, auth_headers: dict[str, str]
    ) -> None:
        """`source-activity` sits under /devices/, where a uuid path param also matches.

        Declared after it, the literal would never be reached and this would 404 or
        422 as a malformed device id instead.
        """
        response = client.get(ENDPOINT.format(user_id=user.id), headers=auth_headers)
        assert response.status_code == 200
        assert "window_days" in response.json()

    def test_an_unknown_user_is_not_an_error(self, client: TestClient, auth_headers: dict[str, str]) -> None:
        """Nothing is leaked either way: a user with no sources and one that does not
        exist both have no sources to report."""
        response = client.get(ENDPOINT.format(user_id=uuid4()), headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["total"] == 0
