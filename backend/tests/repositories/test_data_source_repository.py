"""
Tests for DataSourceRepository.

Regression coverage for the ``data_source.source`` column width: Apple
HealthKit tags on-device data with a source bundle identifier of the form
``com.apple.health.<UUID>`` (53 chars). This overflowed the previous
``VARCHAR(50)`` and aborted the entire SDK import batch with
``StringDataRightTruncation``, so no Apple sleep/records were ever saved.
The column is now ``VARCHAR(100)``.
"""

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import DeviceType, ProviderName
from tests.factories import UserFactory

# Realistic Apple HealthKit source bundle id: "com.apple.health." + a UUID.
APPLE_HEALTH_SOURCE = "com.apple.health.ED447642-08FD-4E45-AF20-633C02C83170"


class TestDataSourceRepository:
    """Test suite for DataSourceRepository."""

    def test_apple_health_source_bundle_id_persists(self, db: Session) -> None:
        """A 53-char Apple source bundle id imports without truncation.

        Exercises ``ensure_data_source`` (the SDK import path that failed) and
        asserts the full identifier round-trips — this would raise
        ``StringDataRightTruncation`` against the old ``VARCHAR(50)`` column.
        """
        # Arrange: a source longer than the old 50-char limit.
        assert len(APPLE_HEALTH_SOURCE) > 50
        user = UserFactory()
        repo = DataSourceRepository(DataSource)

        # Act
        created = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            device_model="Watch7,5",
            source=APPLE_HEALTH_SOURCE,
        )
        db.commit()
        db.expire_all()

        # Assert: stored intact, retrievable by its full identity.
        assert created.source == APPLE_HEALTH_SOURCE
        stored = repo.get_by_identity(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            device_model="Watch7,5",
            source=APPLE_HEALTH_SOURCE,
        )
        assert stored is not None
        assert stored.source == APPLE_HEALTH_SOURCE
        assert len(stored.source) == len(APPLE_HEALTH_SOURCE)


class TestInferDeviceTypeFromSourceLabel:
    """HealthKit stamps the syncing iPhone's productType on watch-recorded samples."""

    def _repo(self) -> DataSourceRepository:
        return DataSourceRepository()

    @pytest.mark.parametrize(
        ("device_model", "source", "expected"),
        [
            # The label names the recorder; the model names the handset that relayed it.
            ("iPhone18,1", "Michael's Apple Watch", DeviceType.WATCH),
            ("iPhone18,1", "Michael's Apple Watch Ultra 3", DeviceType.WATCH),
            ("iPhone18,1", "Oura", DeviceType.RING),
            ("iPhone18,1", "WHOOP", DeviceType.BAND),
            # A phone label under a phone model stays a phone.
            ("iPhone18,1", "Michael's iPhone", DeviceType.PHONE),
            ("iPhone18,1", "Fitness", DeviceType.PHONE),
            ("iPhone18,1", "Health", DeviceType.PHONE),
            # A real wearable model is never downgraded by its label.
            ("Watch7,12", "Bluetooth Device", DeviceType.WATCH),
            ("Watch7,12", "Michael's Apple Watch Ultra 3", DeviceType.WATCH),
            # No model at all: the label carries it.
            (None, "Michael's Apple Watch", DeviceType.WATCH),
        ],
    )
    def test_label_overrides_a_relayed_phone_model(
        self, device_model: str | None, source: str, expected: DeviceType
    ) -> None:
        assert self._repo()._infer_device_type(device_model, "Apple", source) == expected

    def test_falls_back_to_the_brand_when_nothing_else_identifies_the_device(self) -> None:
        assert self._repo()._infer_device_type(None, "Oura", None) == DeviceType.RING
        assert self._repo()._infer_device_type(None, None, None) == DeviceType.UNKNOWN
