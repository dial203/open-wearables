"""Tests for the repair of devices that pooled several writers behind one phone.

The bug this undoes: on an aggregator route ``device_model`` is the handset that synced
the batch, so every app relaying through one iPhone shared the model string and was
grouped into a single device. The script regroups those sources on the writer/model
pair, exactly as ingest now does.

It rewrites existing attributions, which is the one thing the registry is otherwise
careful never to do automatically, so what it must *not* touch is pinned as firmly as
what it must.
See scripts/data_migrations/repair_aggregator_device_pooling.py.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import DataSource, Device, DeviceHistory, DeviceIdentity, User
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import (
    DeviceHistoryAction,
    DeviceIdentityKind,
    IdentityConfidence,
    LabelSource,
    ProviderName,
)
from app.services.devices.identity import IdentityClaim

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "repair_aggregator_device_pooling.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("repair_aggregator_device_pooling", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repair = _load_module().run


def _user(db: Session) -> User:
    user = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    return user


def _source(db: Session, user: User, provider: ProviderName, model: str | None, source: str | None) -> DataSource:
    return DataSourceRepository().ensure_data_source(
        db, user_id=user.id, provider=provider, device_model=model, source=source
    )


def _pool(db: Session, sources: list[DataSource], device: Device) -> None:
    """Put several sources on one device, reproducing the pre-fix state."""
    for ds in sources:
        ds.device_id = device.id
    db.flush()


class TestRegrouping:
    def test_a_pooled_phone_is_split_per_writer(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        oura = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Oura")
        whoop = _source(db, user, ProviderName.APPLE, "iPhone15,3", "WHOOP")

        phone = DeviceRepository().create(db, user_id=user.id, device_type="phone", model_raw="iPhone15,3")
        _pool(db, [muse, oura, whoop], phone)

        result = repair(db, dry_run=False, user_id=user.id)

        db.refresh(muse)
        db.refresh(oura)
        db.refresh(whoop)
        assert len({muse.device_id, oura.device_id, whoop.device_id}) == 3
        assert result["moved"] == 3

    def test_a_dry_run_changes_nothing(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        oura = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Oura")
        phone = DeviceRepository().create(db, user_id=user.id, device_type="phone", model_raw="iPhone15,3")
        _pool(db, [muse, oura], phone)

        result = repair(db, dry_run=True, user_id=user.id)

        db.refresh(muse)
        db.refresh(oura)
        assert muse.device_id == phone.id
        assert oura.device_id == phone.id
        assert result["moved"] == 2  # reported, not written

    def test_the_new_device_is_named_after_the_writer(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        oura = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Oura")
        phone = DeviceRepository().create(db, user_id=user.id, device_type="phone", model_raw="iPhone15,3")
        _pool(db, [muse, oura], phone)

        repair(db, dry_run=False, user_id=user.id)

        db.refresh(muse)
        device = DeviceRepository().get(db, muse.device_id)
        assert device.label == "Muse"
        assert device.model_raw == "iPhone15,3"  # what the provider said, kept verbatim

    def test_every_move_is_in_the_audit_trail(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        oura = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Oura")
        phone = DeviceRepository().create(db, user_id=user.id, device_type="phone", model_raw="iPhone15,3")
        _pool(db, [muse, oura], phone)

        repair(db, dry_run=False, user_id=user.id)

        entries = db.query(DeviceHistory).filter(DeviceHistory.data_source_id.in_([muse.id, oura.id])).all()
        # Ingest already wrote a `linked` row for each source, so the script's own moves
        # are the ones carrying its actor - that is what has to be reconstructable.
        by_script = [
            e
            for e in entries
            if e.action == DeviceHistoryAction.LINKED.value and e.actor == "script:repair_aggregator_device_pooling"
        ]
        assert {e.data_source_id for e in by_script} == {muse.id, oura.id}
        assert all(e.reason for e in by_script)

    def test_stale_model_string_claims_are_removed(self, db: Session) -> None:
        """Left behind, they would re-pool everything on the next sync."""
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        phone = DeviceRepository().create(db, user_id=user.id, device_type="phone", model_raw="iPhone15,3")
        DeviceRepository().add_claim(
            db,
            phone,
            IdentityClaim(
                route=ProviderName.APPLE.value,
                kind=DeviceIdentityKind.MODEL_STRING,
                value="iPhone15,3",
                confidence=IdentityConfidence.WEAK,
            ),
        )
        _pool(db, [muse], phone)

        repair(db, dry_run=False, user_id=user.id)

        remaining = {
            (c.id_kind, c.id_value) for c in db.query(DeviceIdentity).filter(DeviceIdentity.user_id == user.id).all()
        }
        assert (DeviceIdentityKind.MODEL_STRING.value, "iPhone15,3") not in remaining


class TestWhatItMustNotTouch:
    def test_a_direct_route_is_left_alone(self, db: Session) -> None:
        user = _user(db)
        activities = _source(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        wellness = _source(db, user, ProviderName.GARMIN, "fenix 8", "connect")
        before = activities.device_id

        repair(db, dry_run=False, user_id=user.id)

        db.refresh(activities)
        db.refresh(wellness)
        assert activities.device_id == before
        assert activities.device_id == wellness.device_id

    def test_a_deliberately_detached_source_stays_detached(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        muse.device_id = None
        muse.attribution_locked_at = datetime.now(UTC)
        db.flush()

        result = repair(db, dry_run=False, user_id=user.id)

        db.refresh(muse)
        assert muse.device_id is None
        assert result["skipped_locked"] == 1

    def test_a_source_a_person_linked_by_hand_is_left_where_they_put_it(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        chosen = DeviceRepository().create(db, user_id=user.id, device_type="other", label="Muse S, left temple")
        DeviceRepository().attach_data_source(db, muse, chosen, actor="dial@osu.edu", reason="it is the headband")

        result = repair(db, dry_run=False, user_id=user.id)

        db.refresh(muse)
        assert muse.device_id == chosen.id
        assert result["skipped_hand_linked"] == 1

    def test_a_device_someone_named_is_never_pruned(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        named = DeviceRepository().create(
            db,
            user_id=user.id,
            device_type="phone",
            model_raw="iPhone15,3",
            label="Study phone 04",
            label_source=LabelSource.MANUAL,
        )
        _pool(db, [muse], named)

        repair(db, dry_run=False, user_id=user.id, prune_empty=True)

        assert db.get(Device, named.id) is not None

    def test_rerunning_is_a_no_op(self, db: Session) -> None:
        user = _user(db)
        muse = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        oura = _source(db, user, ProviderName.APPLE, "iPhone15,3", "Oura")
        phone = DeviceRepository().create(db, user_id=user.id, device_type="phone", model_raw="iPhone15,3")
        _pool(db, [muse, oura], phone)

        repair(db, dry_run=False, user_id=user.id)
        second = repair(db, dry_run=False, user_id=user.id)

        assert second["moved"] == 0
        assert second["created"] == 0
