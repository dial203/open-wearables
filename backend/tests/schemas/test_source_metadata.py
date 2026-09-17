"""SourceMetadata.from_data_source — the join identity exposed to consumers."""

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.orm import Session

from app.schemas.utils import SourceMetadata


def _fake_data_source(**overrides: object) -> SimpleNamespace:
    base = {
        "id": uuid4(),
        "provider": "apple",
        "source": "com.oura.oura",
        "device_model": "iPhone18,1",
        "original_source_name": "Oura",
        "device_type": "phone",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_from_data_source_exposes_join_identity() -> None:
    ds = _fake_data_source()
    meta = SourceMetadata.from_data_source(ds)

    # data_source_id must be the SAME value as /data-sources items[].id so the two
    # payloads can be joined directly.
    assert meta.data_source_id == ds.id
    assert meta.ingestion_provider == "apple"
    assert meta.source_tag == "com.oura.oura"
    assert meta.original_source_name == "Oura"
    assert meta.device_type == "phone"
    assert meta.device == "iPhone18,1"


def test_explicit_fields_alias_provider_and_source() -> None:
    """`provider`/`source` carry the ingestion path and the writer inside it.

    Before 0.7.0 `provider` carried the sub-source tag, which is why the explicit
    `ingestion_provider`/`source_tag` fields exist. They now duplicate `provider`
    and `source` exactly, and stay as stable names for consumers pinned to them.
    """
    meta = SourceMetadata.from_data_source(_fake_data_source())
    assert meta.provider == "apple"
    assert meta.source == "com.oura.oura"
    assert meta.ingestion_provider == meta.provider
    assert meta.source_tag == meta.source

    # A source with no provider still yields a non-null `provider` for consumers
    # that require one, while the explicit field reports the real None.
    no_provider = SourceMetadata.from_data_source(_fake_data_source(provider=None))
    assert no_provider.provider == "unknown"
    assert no_provider.ingestion_provider is None

    # A source with no sub-source tag reports None on both, not "unknown".
    no_source = SourceMetadata.from_data_source(_fake_data_source(source=None))
    assert no_source.source is None
    assert no_source.source_tag is None


def test_nullable_fields_tolerated() -> None:
    meta = SourceMetadata.from_data_source(
        _fake_data_source(device_model=None, original_source_name=None, device_type=None, provider=None)
    )
    assert meta.device is None
    assert meta.ingestion_provider is None
    assert meta.original_source_name is None
    assert meta.device_type is None


class TestDeviceAttribution:
    """device_id is the physical unit; data_source_id is the path it arrived by.

    Downstream consumers need both: group on device_id to pool one device's data,
    keep data_source_id to tell a direct read from an aggregator relay, since the
    relay changes freshness, rounding and completeness.
    """

    def test_device_id_is_exposed_when_attributed(self) -> None:
        from types import SimpleNamespace
        from uuid import uuid4

        from app.schemas.enums import ProviderName
        from app.schemas.utils.metadata import SourceMetadata

        device_id = uuid4()
        data_source = SimpleNamespace(
            id=uuid4(),
            provider=ProviderName.OURA,
            source="oura",
            device_model="Oura Ring Gen3",
            device_type="ring",
            original_source_name="Oura",
            device_id=device_id,
        )

        meta = SourceMetadata.from_data_source(data_source)
        assert meta.device_id == device_id

    def test_unattributed_source_reports_no_device(self) -> None:
        """Unattributed is a normal resting state, not an error."""
        from types import SimpleNamespace
        from uuid import uuid4

        from app.schemas.enums import ProviderName
        from app.schemas.utils.metadata import SourceMetadata

        data_source = SimpleNamespace(
            id=uuid4(),
            provider=ProviderName.WHOOP,
            source="whoop",
            device_model=None,
            device_type="unknown",
            original_source_name="Whoop",
            device_id=None,
        )

        meta = SourceMetadata.from_data_source(data_source)
        assert meta.device_id is None
        assert meta.device_label is None

    def test_label_is_omitted_rather_than_lazy_loaded(self, db: Session) -> None:
        """This runs per sample on hot read paths.

        A lazy load to fetch the label would turn one timeseries response into a query
        per row, so a caller that has not joined the device gets the id and no label.
        """
        from uuid import uuid4

        from app.models import User
        from app.repositories.data_source_repository import DataSourceRepository
        from app.repositories.device_repository import DeviceRepository
        from app.schemas.enums import ProviderName
        from app.schemas.utils.metadata import SourceMetadata

        user = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
        db.add(user)
        db.flush()

        source = DataSourceRepository().ensure_data_source(
            db, user_id=user.id, provider=ProviderName.GARMIN, device_model="fenix 8", source="garmin"
        )
        device = DeviceRepository().get(db, source.device_id)
        DeviceRepository().update_fields(db, device, {"label": "Sub 04 fenix"})
        db.commit()
        db.expire_all()

        fresh = DataSourceRepository().get(db, source.id)
        meta = SourceMetadata.from_data_source(fresh)
        assert meta.device_id == device.id
        assert meta.device_label is None

        # Joined explicitly, the label comes through.
        _ = fresh.device
        assert SourceMetadata.from_data_source(fresh).device_label == "Sub 04 fenix"
