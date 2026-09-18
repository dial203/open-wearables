"""The backfill for devices that were built from the phone that relayed their data.

Before detection recognised a relaying host, every third-party app writing through
HealthKit on one handset reported the same model string, so all of them landed on a
single device named after the phone. Editing it renamed every brand behind it at once.

These pin that an existing database ends up where a fresh one would: one device per
writing app, the phone kept as provenance, and no claim left describing the handset.
See scripts/data_migrations/split_host_relayed_devices.py.
"""

import importlib.util
from collections import defaultdict
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import DataSource, Device, DeviceIdentity, User
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import DeviceIdentityKind, DeviceType, IdentityConfidence, LabelSource, ProviderName

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "split_host_relayed_devices.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("split_host_relayed_devices", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load_module().run


def _user(db: Session) -> User:
    user = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    return user


def _pooled_phone_device(db: Session, user: User, writers: list[str]) -> Device:
    """Rebuild what the old within-route model grouping produced.

    One device carrying the phone as its model, every writing app's data source
    attributed to it, and a model-string claim naming the handset.
    """
    repo = DeviceRepository()
    device = repo.create(
        db,
        user_id=user.id,
        device_type=DeviceType.PHONE.value,
        brand="Apple",
        model_raw="iPhone 17 Pro",
        actor="test",
        detected=True,
    )
    db.add(
        DeviceIdentity(
            id=uuid4(),
            user_id=user.id,
            device_id=device.id,
            route=ProviderName.APPLE.value,
            id_kind=DeviceIdentityKind.MODEL_STRING.value,
            id_value="iPhone 17 Pro",
            confidence=IdentityConfidence.WEAK.value,
        )
    )
    for writer in writers:
        data_source = DataSource(
            id=uuid4(),
            user_id=user.id,
            provider=ProviderName.APPLE,
            source=writer,
            device_model="iPhone 17 Pro",
            device_type=DeviceType.PHONE.value,
            device_id=device.id,
        )
        db.add(data_source)
        db.add(
            DeviceIdentity(
                id=uuid4(),
                user_id=user.id,
                device_id=device.id,
                route=ProviderName.APPLE.value,
                id_kind=DeviceIdentityKind.HEALTHKIT_BUNDLE.value,
                id_value=writer,
                confidence=IdentityConfidence.WEAK.value,
            )
        )
    db.flush()
    return device


def _devices(db: Session, user: User) -> list[Device]:
    return DeviceRepository().list_for_user(db, user.id)


def test_one_device_per_writing_app(db: Session) -> None:
    user = _user(db)
    _pooled_phone_device(db, user, ["Muse", "Oura", "WHOOP"])

    assert run(db, dry_run=False) == 1

    devices = _devices(db, user)
    assert len(devices) == 3
    by_source = {ds.source: ds.device_id for ds in db.query(DataSource).filter(DataSource.user_id == user.id).all()}
    assert len({by_source["Muse"], by_source["Oura"], by_source["WHOOP"]}) == 3


def test_the_phone_moves_to_provenance_and_the_brand_is_re_derived(db: Session) -> None:
    user = _user(db)
    _pooled_phone_device(db, user, ["Muse", "Oura"])
    run(db, dry_run=False)

    by_label = {d.label: d for d in _devices(db, user)}
    muse = by_label["Muse"]
    assert muse.model_raw is None
    assert muse.host_model_raw == "iPhone 17 Pro"
    assert muse.brand == "Muse"
    assert muse.device_type == DeviceType.EEG.value
    assert muse.label_source == LabelSource.AUTO.value
    assert by_label["Oura"].device_type == DeviceType.RING.value


def test_no_claim_is_left_describing_the_handset(db: Session) -> None:
    """A surviving host claim collides with the next sync and raises a false proposal."""
    user = _user(db)
    _pooled_phone_device(db, user, ["Muse", "Oura"])
    run(db, dry_run=False)

    claims = db.query(DeviceIdentity).filter(DeviceIdentity.user_id == user.id).all()
    # The writer's own claim, plus the key detection groups by: that key names the host,
    # but only bound to one writer, so no other app on the phone can produce it. What
    # must not survive is a claim on the bare handset string.
    assert "iPhone 17 Pro" not in {c.id_value for c in claims}
    assert {c.id_value for c in claims} == {
        "Muse",
        "Oura",
        "Muse||iPhone 17 Pro",
        "Oura||iPhone 17 Pro",
    }
    # Every claim sits on the device holding that writer's data source.
    writers_by_device: dict[object, set[str]] = defaultdict(set)
    for claim in claims:
        writers_by_device[claim.device_id].add(claim.id_value.split("||")[0])
    for data_source in db.query(DataSource).filter(DataSource.user_id == user.id).all():
        assert writers_by_device[data_source.device_id] == {data_source.source}


def test_a_hand_set_name_is_never_overwritten(db: Session) -> None:
    user = _user(db)
    device = _pooled_phone_device(db, user, ["Muse"])
    DeviceRepository().update_fields(db, device, {"label": "Sub 04 headband"}, actor="test")

    run(db, dry_run=False)

    assert _devices(db, user)[0].label == "Sub 04 headband"


def test_sources_the_provider_really_described_stay_put(db: Session) -> None:
    """A device holding both kinds keeps the real hardware and sheds the relayed apps."""
    user = _user(db)
    device = _pooled_phone_device(db, user, ["Muse"])
    watch = DataSource(
        id=uuid4(),
        user_id=user.id,
        provider=ProviderName.APPLE,
        source="Ali's Watch",
        device_model="Apple Watch Ultra 3 49mm",
        device_type=DeviceType.WATCH.value,
        device_id=device.id,
    )
    db.add(watch)
    db.flush()

    run(db, dry_run=False)

    db.refresh(watch)
    assert watch.device_id == device.id
    assert device.model_raw == "iPhone 17 Pro"
    muse_source = db.query(DataSource).filter(DataSource.source == "Muse").one()
    assert muse_source.device_id != device.id


def test_a_device_the_provider_named_is_untouched(db: Session) -> None:
    user = _user(db)
    repo = DeviceRepository()
    device = repo.create(
        db,
        user_id=user.id,
        device_type=DeviceType.WATCH.value,
        brand="Garmin",
        model_raw="fenix 8",
        actor="test",
    )
    db.add(
        DataSource(
            id=uuid4(),
            user_id=user.id,
            provider=ProviderName.GARMIN,
            source="garmin",
            device_model="fenix 8",
            device_type=DeviceType.WATCH.value,
            device_id=device.id,
        )
    )
    db.flush()

    assert run(db, dry_run=False) == 0
    assert device.model_raw == "fenix 8"


def test_dry_run_changes_nothing(db: Session) -> None:
    user = _user(db)
    _pooled_phone_device(db, user, ["Muse", "Oura"])

    assert run(db, dry_run=True) == 1
    assert len(_devices(db, user)) == 1


def test_re_running_is_a_no_op(db: Session) -> None:
    user = _user(db)
    _pooled_phone_device(db, user, ["Muse", "Oura"])
    run(db, dry_run=False)

    assert run(db, dry_run=False) == 0
    assert len(_devices(db, user)) == 2
