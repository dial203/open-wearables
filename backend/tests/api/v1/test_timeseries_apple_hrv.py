"""Apple Watch HRV reaches /timeseries with the window each value summarises.

HealthKit states an HRV sample's window as its start and end. An Ultra 4 on watchOS 27
writes Recovery HRV (RMSSD) and SDNN over the same windows, mostly 300 s but not always,
so a consumer cutting a chest-strap recording to the same span has to be told each
window's length rather than assume five minutes. This pins that the SDK path keeps it,
next to the algorithm version HealthKit tags the value with.
"""

import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.services.sdk.import_service import ImportService

WATCH = {"name": "MD Apple Watch Ultra 4", "bundleIdentifier": "com.apple.health.test", "productType": "Watch8,1"}


@pytest.fixture(autouse=True)
def mock_sleep_redis() -> Any:
    with (
        patch("app.services.sdk.sleep_service.get_redis_client") as mock_get_redis,
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps"),
    ):
        mock_get_redis.return_value = MagicMock()
        yield


def _record(metric: str, value: float, start: str, end: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": f"{metric}-{start}",
        "type": f"HKQuantityTypeIdentifier{metric}",
        "value": value,
        "unit": "ms" if metric.startswith("HeartRateVariability") else "count/min",
        "startDate": start,
        "endDate": end,
        "source": WATCH,
        **extra,
    }


def _save(db: Session, user: User) -> None:
    records = [
        _record(
            "HeartRateVariabilityRMSSD",
            42.0,
            "2026-09-29T07:00:00Z",
            "2026-09-29T07:05:00Z",
            metadata={"HKAlgorithmVersion": "3"},
        ),
        _record("HeartRateVariabilitySDNN", 51.5, "2026-09-29T07:00:00Z", "2026-09-29T07:05:00Z"),
        # A short window: the length must come from the sample, not be assumed.
        _record("HeartRateVariabilityRMSSD", 25.0, "2026-09-29T07:10:00Z", "2026-09-29T07:11:00Z"),
        _record("HeartRate", 55.0, "2026-09-29T07:12:00Z", "2026-09-29T07:12:00Z"),
    ]
    payload = {
        "provider": "apple",
        "sdkVersion": "0.15.0",
        "syncTimestamp": "2026-09-29T08:00:00Z",
        "data": {"records": records},
    }
    ImportService(log=logging.getLogger("test")).load_data(db, payload, str(user.id))


def _get(client: TestClient, user: User, headers: dict[str, str], types: str) -> list[dict]:
    response = client.get(
        f"/api/v1/users/{user.id}/timeseries?types={types}"
        "&start_time=2026-09-29T06:00:00Z&end_time=2026-09-29T08:00:00Z&resolution=raw&limit=100",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


class TestAppleHrvWindows:
    def test_recovery_hrv_states_each_window_length(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        _save(db, user)

        rows = _get(client, user, api_key_header, "heart_rate_variability_rmssd")

        assert [(r["timestamp"][:19], r["value"], r["interval_seconds"]) for r in rows] == [
            ("2026-09-29T07:00:00", 42.0, 300),
            ("2026-09-29T07:10:00", 25.0, 60),
        ]

    def test_sdnn_from_the_same_window_states_the_same_length(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        _save(db, user)

        rows = _get(client, user, api_key_header, "heart_rate_variability_sdnn")

        assert [(r["timestamp"][:19], r["interval_seconds"]) for r in rows] == [("2026-09-29T07:00:00", 300)]

    def test_an_instantaneous_heart_rate_states_no_window(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        _save(db, user)

        rows = _get(client, user, api_key_header, "heart_rate")

        assert [r["interval_seconds"] for r in rows] == [None]
