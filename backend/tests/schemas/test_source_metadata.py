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
