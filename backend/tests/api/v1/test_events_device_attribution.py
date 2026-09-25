"""A sleep session names the device its source is filed under.

The dashboard links an unidentified source to a device straight from the session
row, so the row has to pick the name up once the link exists. Garmin sends no
device model with sleep, which is the case this is written around: before the link
the row has nothing to call the unit, and after it the only name is the registry's.
"""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import DeviceType, ProviderName
from tests.factories import ApiKeyFactory, EventRecordFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers

_WINDOW = {"start_date": "2025-12-25T00:00:00Z", "end_date": "2025-12-27T00:00:00Z"}


def _sessions(client: TestClient, user_id: object) -> list[dict]:
    response = client.get(
        f"/api/v1/users/{user_id}/events/sleep",
        headers=api_key_headers(ApiKeyFactory().plain_key),
        params=_WINDOW,
    )
    assert response.status_code == 200
    return response.json()["data"]


def test_a_linked_model_less_source_is_named_on_its_sessions(
    client: TestClient, db: Session, auth_headers: dict[str, str]
) -> None:
    user = UserFactory()
    connection = UserConnectionFactory(user=user, provider="garmin", account_email="p01@lab.example.edu")
    data_source = DataSourceRepository().ensure_data_source(
        db,
        user_id=user.id,
        provider=ProviderName.GARMIN,
        device_model=None,
        source="garmin",
        user_connection_id=connection.id,
    )
    db.commit()
    EventRecordFactory(
        mapping=data_source,
        category="sleep",
        start_datetime=datetime(2025, 12, 25, 23, 0, 0, tzinfo=timezone.utc),
        end_datetime=datetime(2025, 12, 26, 6, 0, 0, tzinfo=timezone.utc),
        duration_seconds=25200,
    )

    before = _sessions(client, user.id)[0]["source"]
    assert before["device_id"] is None
    assert before["device_display_name"] is None
    # What the row links by, and which login it came through.
    assert before["data_source_id"] == str(data_source.id)
    assert before["user_connection_id"] == str(connection.id)

    device = client.post(
        f"/api/v1/users/{user.id}/devices",
        headers=auth_headers,
        json={"device_type": DeviceType.WATCH.value, "brand": "Garmin", "label": "Sub 01 Venu X1"},
    ).json()
    linked = client.post(
        f"/api/v1/users/{user.id}/devices/{device['id']}/link",
        headers=auth_headers,
        json={"data_source_id": str(data_source.id), "reason": "only watch on this account"},
    )
    assert linked.status_code == 200

    after = _sessions(client, user.id)[0]["source"]
    assert after["device_id"] == device["id"]
    assert after["device_display_name"] == "Sub 01 Venu X1"
    assert after["device_label"] == "Sub 01 Venu X1"
    assert after["device_type"] == "watch"
