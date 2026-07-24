"""SourceMetadata.from_data_source — the join identity exposed to consumers."""

from types import SimpleNamespace
from uuid import uuid4

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


def test_legacy_fields_unchanged() -> None:
    """`provider` keeps carrying the sub-source tag (with the "unknown" fallback)."""
    assert SourceMetadata.from_data_source(_fake_data_source()).provider == "com.oura.oura"
    assert SourceMetadata.from_data_source(_fake_data_source(source=None)).provider == "unknown"
    # ...and the explicit field still reports the real sub-source (None, not "unknown")
    assert SourceMetadata.from_data_source(_fake_data_source(source=None)).source_tag is None


def test_nullable_fields_tolerated() -> None:
    meta = SourceMetadata.from_data_source(
        _fake_data_source(device_model=None, original_source_name=None, device_type=None, provider=None)
    )
    assert meta.device is None
    assert meta.ingestion_provider is None
    assert meta.original_source_name is None
    assert meta.device_type is None
