"""Health Connect declares a device type. Ingest must use it rather than re-infer one.

Health Connect's ``Metadata`` may carry a ``Device`` with a manufacturer, a model and
a type enum, so a writer that fills it in has said what kind of hardware produced the
samples. HealthKit's ``HKDevice`` has no equivalent field, so on the Apple route there
is nothing to consume and classification stays inference over model strings.

That matters most on a relayed stream, where the model string names the phone that
ran the writing app: inference there can only ever answer "phone", while the platform
report answers "ring".
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, User
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import DeviceType, ProviderName
from app.schemas.providers.mobile_sdk import SourceInfo
from app.services.devices.detection import DeviceDetectionService
from app.services.sdk.device_resolution import extract_reported_device_type
from tests.factories import UserConnectionFactory


@pytest.fixture
def user(db: Session) -> User:
    user = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    return user


def _ensure(
    db: Session, user: User, provider: ProviderName, device_model: str | None, source: str | None
) -> DataSource:
    return DataSourceRepository().ensure_data_source(
        db, user_id=user.id, provider=provider, device_model=device_model, source=source
    )


def _resolve(db: Session, data_source: DataSource, reported: DeviceType | None) -> None:
    DeviceDetectionService().resolve_for_data_source(db, data_source, reported_device_type=reported)


class TestExtractionFromTheSdkPayload:
    def test_health_connect_device_type_survives_ingest(self) -> None:
        source = SourceInfo(appId="com.ouraring.oura", deviceModel="Pixel 9 Pro", deviceType="ring")
        assert extract_reported_device_type(source) is DeviceType.RING

    def test_a_healthkit_source_declares_nothing(self) -> None:
        """HKDevice has name, manufacturer, model and versions - and no category."""
        source = SourceInfo(bundleIdentifier="com.ouraring.oura", productType="iPhone18,1")
        assert extract_reported_device_type(source) is None

    def test_no_source_at_all_declares_nothing(self) -> None:
        assert extract_reported_device_type(None) is None


class TestTheReportCorrectsARelayedStream:
    """The case the model string cannot answer: a ring relayed through a handset."""

    def test_a_reported_ring_beats_the_phone_the_model_names(self, db: Session, user: User) -> None:
        """A package name the brand tables do not know leaves inference with the host.

        ``com.ouraring.oura`` would be no test of this: "oura" is in the keyword table,
        so the writer name alone already types it. ``com.ultrahuman.app`` is not, which
        is the general case - the table can only ever hold the brands someone added.
        """
        source = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.ultrahuman.app")
        assert source.device_type == DeviceType.PHONE.value  # inference, from the host

        _resolve(db, source, DeviceType.RING)

        assert source.device_type == DeviceType.RING.value
        assert DeviceRepository().get(db, source.device_id).device_type == DeviceType.RING.value

    def test_the_host_is_still_kept_as_provenance(self, db: Session, user: User) -> None:
        """Classifying the unit must not start claiming the phone was the unit."""
        source = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.ultrahuman.app")
        _resolve(db, source, DeviceType.RING)
        device = DeviceRepository().get(db, source.device_id)

        assert device.model_raw is None
        assert device.host_model_raw == "Pixel 9 Pro"

    def test_a_reported_phone_on_a_relayed_stream_is_dropped(self, db: Session, user: User) -> None:
        """Reaching the relay branch means the model already named the host.

        A report agreeing it is a phone describes that same carrier, so trusting it
        would classify the relayed device as the handset it was relayed through.
        """
        source = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.whoop.android")
        _resolve(db, source, DeviceType.PHONE)
        device = DeviceRepository().get(db, source.device_id)

        assert device.host_model_raw == "Pixel 9 Pro"
        assert device.device_type != DeviceType.PHONE.value

    def test_two_writers_on_one_phone_stay_two_devices(self, db: Session, user: User) -> None:
        """The report classifies; it must not become a grouping key.

        Two rings relayed through one handset both report RING, and pooling on that
        would be the over-merge the whole package exists to prevent.
        """
        oura = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.ouraring.oura")
        ultrahuman = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.ultrahuman.app")
        _resolve(db, oura, DeviceType.RING)
        _resolve(db, ultrahuman, DeviceType.RING)

        assert oura.device_id != ultrahuman.device_id


class TestPrecedence:
    def test_a_declared_sensor_still_wins(self, db: Session, user: User) -> None:
        """A person's assertion about the instrument worn outranks the writer's.

        A Health Connect writer knows no more about it than HealthKit does: an app
        paired with a chest strap through a watch reports the watch, because the
        watch is what it talks to.
        """
        connection = UserConnectionFactory(
            user=user,
            provider=ProviderName.HEALTH_CONNECT.value,
            provider_user_id="acct-1",
            sensor_label="Polar H10",
        )
        db.flush()
        source = DataSourceRepository().ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.HEALTH_CONNECT,
            user_connection_id=connection.id,
            device_model="Pixel 9 Pro",
            source="com.polar.polarflow",
        )
        assert source.device_type == DeviceType.CHEST_STRAP.value

        _resolve(db, source, DeviceType.WATCH)

        assert source.device_type == DeviceType.CHEST_STRAP.value

    def test_an_eeg_model_refines_a_reported_head_mounted(self, db: Session, user: User) -> None:
        """Health Connect says where it sits; only the model says what it measures."""
        source = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Muse S Athena", "com.choosemuse.muse")
        _resolve(db, source, DeviceType.HEADBAND)

        assert source.device_type == DeviceType.EEG.value

    def test_declaring_nothing_leaves_inference_alone(self, db: Session, user: User) -> None:
        """Apple syncs pass None here, and must not be downgraded by it."""
        source = _ensure(db, user, ProviderName.APPLE, "Watch7,12", "Michael's Apple Watch")
        assert source.device_type == DeviceType.WATCH.value

        _resolve(db, source, None)

        assert source.device_type == DeviceType.WATCH.value


class TestApplicationIsIndependentOfAttribution:
    def test_a_detached_source_is_still_classified(self, db: Session, user: User) -> None:
        """The type describes the source; the lock only governs which device it joins."""
        source = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.ultrahuman.app")
        object.__setattr__(source, "device_id", None)
        object.__setattr__(source, "attribution_locked_at", datetime.now(UTC))
        db.flush()

        _resolve(db, source, DeviceType.RING)

        assert source.device_type == DeviceType.RING.value
        assert source.device_id is None

    def test_re_reporting_the_same_type_is_idempotent(self, db: Session, user: User) -> None:
        source = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.ultrahuman.app")
        for _ in range(3):
            _resolve(db, source, DeviceType.RING)

        assert source.device_type == DeviceType.RING.value
        assert len(DeviceRepository().list_for_user(db, user.id)) == 1
        assert DeviceRepository().pending_proposals(db, user.id) == []
