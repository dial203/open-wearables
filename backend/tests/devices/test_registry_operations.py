"""Hand edits, merge, split, retire - and the audit trail each of them leaves.

Device attribution decides which physical unit a sample is credited to, so an edit
months after ingest rewrites the provenance of historical data. These tests pin that
every such change is recoverable from history, including the destructive one.
"""

from datetime import UTC, datetime, timedelta
from logging import getLogger
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, Device, DeviceHistory, User
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import DeviceHistoryAction, DeviceType, LabelSource, ProviderName
from app.services.device_service import DeviceService


@pytest.fixture
def user(db: Session) -> User:
    user = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def repo() -> DeviceRepository:
    return DeviceRepository()


def _ensure(db: Session, user: User, provider: ProviderName, model: str | None, source: str | None) -> DataSource:
    return DataSourceRepository().ensure_data_source(
        db, user_id=user.id, provider=provider, device_model=model, source=source
    )


def _actions(db: Session, user: User) -> list[str]:
    # The session runs with autoflush off, so flush first or pending history rows are
    # invisible and a count taken here measures flush timing rather than behaviour.
    db.flush()
    return [h.action for h in db.query(DeviceHistory).filter(DeviceHistory.user_id == user.id).all()]


class TestHandEditing:
    def test_editing_a_field_records_both_values(self, db: Session, user: User, repo: DeviceRepository) -> None:
        device = repo.create(db, user_id=user.id, device_type=DeviceType.WATCH, model_raw="fenix 8")
        repo.update_fields(db, device, {"label": "Sub 04 fenix"}, actor="dial@osu.edu", reason="study labelling")

        entry = (
            db.query(DeviceHistory)
            .filter(DeviceHistory.device_id == device.id, DeviceHistory.action == DeviceHistoryAction.UPDATED.value)
            .one()
        )
        assert entry.field == "label"
        assert entry.old_value is None
        assert entry.new_value == "Sub 04 fenix"
        assert entry.actor == "dial@osu.edu"
        assert entry.reason == "study labelling"

    def test_a_hand_set_label_pins_the_device(self, db: Session, user: User, repo: DeviceRepository) -> None:
        """Detection must never overwrite a label a person chose.

        A confidently wrong auto-label is worse than no label: nothing about it
        signals that it is wrong.
        """
        device = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        assert device.label_source == LabelSource.AUTO.value

        repo.update_fields(db, device, {"label": "left index"})
        assert device.label_source == LabelSource.MANUAL.value

    def test_no_history_when_nothing_changed(self, db: Session, user: User, repo: DeviceRepository) -> None:
        device = repo.create(db, user_id=user.id, device_type=DeviceType.WATCH, label="fenix")
        before = len(_actions(db, user))
        repo.update_fields(db, device, {"label": "fenix"})
        assert len(_actions(db, user)) == before

    def test_provider_reported_fields_are_not_editable(self, db: Session, user: User, repo: DeviceRepository) -> None:
        """model_raw is the record of what the provider claimed, not our opinion of it.

        Editing it in place would erase the only evidence of what the hardware said
        it was. Display normalization has its own field.
        """
        device = repo.create(db, user_id=user.id, device_type=DeviceType.WATCH, model_raw="fenix 8")
        with pytest.raises(ValueError, match="not editable"):
            repo.update_fields(db, device, {"model_raw": "Garmin fenix 8 Solar"})
        with pytest.raises(ValueError, match="not editable"):
            repo.update_fields(db, device, {"brand": "Not Garmin"})


class TestRetirement:
    def test_retiring_records_the_effective_date(self, db: Session, user: User, repo: DeviceRepository) -> None:
        """A warranty replacement is identical on every provider-reported field.

        Without the date that the old unit stopped being worn, pre- and post-swap
        samples pool into one device and the swap disappears from the analysis.
        """
        device = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        swap_date = datetime.now(UTC) - timedelta(days=30)

        repo.set_retired(db, device, retired=True, effective_at=swap_date, reason="warranty replacement")

        assert device.is_active is False
        assert device.retired_at == swap_date
        assert DeviceHistoryAction.RETIRED.value in _actions(db, user)

    def test_reactivating_clears_the_date(self, db: Session, user: User, repo: DeviceRepository) -> None:
        device = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        repo.set_retired(db, device, retired=True)
        repo.set_retired(db, device, retired=False)

        assert device.is_active is True
        assert device.retired_at is None


class TestMerge:
    def test_merge_moves_sources_and_records_enough_to_undo_it(
        self, db: Session, user: User, repo: DeviceRepository
    ) -> None:
        """Merge is the irreversible direction, so the undo has to be in the history.

        The absorbed row is deleted, and its FK on the history entry nulls out with
        it, so everything needed to rebuild it is written as plain values first.
        """
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        via_apple = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        keep = repo.get(db, direct.device_id)
        absorb = repo.get(db, via_apple.device_id)
        absorbed_id = absorb.id

        repo.merge(db, keep=keep, absorb=absorb, actor="dial@osu.edu", reason="same ring, confirmed by overlap")

        assert db.get(Device, absorbed_id) is None
        db.refresh(via_apple)
        assert via_apple.device_id == keep.id
        # The sources are re-pointed, never combined: direct and relayed data differ
        # in freshness and rounding, so the comparison has to survive the merge.
        assert {ds.id for ds in repo.data_sources_for_device(db, keep.id)} == {direct.id, via_apple.id}

        entry = (
            db.query(DeviceHistory)
            .filter(DeviceHistory.action == DeviceHistoryAction.MERGED.value, DeviceHistory.user_id == user.id)
            .one()
        )
        assert entry.meta["absorbed"]["id"] == str(absorbed_id)
        assert entry.meta["absorbed"]["model_raw"] == "Oura Ring Gen3"
        assert str(via_apple.id) in entry.meta["moved_data_source_ids"]

    def test_merge_keeps_the_union_of_the_service_spans(self, db: Session, user: User, repo: DeviceRepository) -> None:
        keep = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        absorb = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        old = datetime.now(UTC) - timedelta(days=400)
        keep.first_seen_at = datetime.now(UTC) - timedelta(days=10)
        absorb.first_seen_at = old
        db.flush()

        repo.merge(db, keep=keep, absorb=absorb)
        assert keep.first_seen_at == old

    def test_a_hand_set_label_survives_a_merge(self, db: Session, user: User, repo: DeviceRepository) -> None:
        keep = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        absorb = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        repo.update_fields(db, absorb, {"label": "left index"})

        repo.merge(db, keep=keep, absorb=absorb)
        assert keep.label == "left index"
        assert keep.label_source == LabelSource.MANUAL.value

    def test_cannot_merge_across_users(self, db: Session, user: User, repo: DeviceRepository) -> None:
        other = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
        db.add(other)
        db.flush()

        mine = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        theirs = repo.create(db, user_id=other.id, device_type=DeviceType.RING)
        with pytest.raises(ValueError, match="different users"):
            repo.merge(db, keep=mine, absorb=theirs)

    def test_cannot_merge_a_device_into_itself(self, db: Session, user: User, repo: DeviceRepository) -> None:
        device = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        with pytest.raises(ValueError, match="into itself"):
            repo.merge(db, keep=device, absorb=device)


class TestSplit:
    def test_split_moves_the_named_sources_to_a_new_device(
        self, db: Session, user: User, repo: DeviceRepository
    ) -> None:
        """The undo for a merge, and the fix for detection that grouped too eagerly."""
        first = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        second = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "connect")
        assert first.device_id == second.device_id
        original = repo.get(db, first.device_id)

        new_device = repo.split(db, original, [second.id], actor="dial@osu.edu", reason="two identical watches")

        db.refresh(second)
        assert second.device_id == new_device.id
        assert first.device_id == original.id
        assert new_device.model_raw == original.model_raw
        # The label named whatever stayed behind, so it does not follow the split.
        assert new_device.label is None
        assert DeviceHistoryAction.SPLIT.value in _actions(db, user)

    def test_splitting_everything_off_is_refused(self, db: Session, user: User, repo: DeviceRepository) -> None:
        """That is a rename, and it would leave a device with no data behind it."""
        only = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        device = repo.get(db, only.device_id)
        with pytest.raises(ValueError, match="empty device"):
            repo.split(db, device, [only.id])

    def test_splitting_a_foreign_source_is_refused(self, db: Session, user: User, repo: DeviceRepository) -> None:
        mine = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        other = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        device = repo.get(db, mine.device_id)
        with pytest.raises(ValueError, match="belong to this device"):
            repo.split(db, device, [other.id])


class TestProposals:
    def test_a_rejected_pair_is_never_proposed_again(self, db: Session, user: User, repo: DeviceRepository) -> None:
        """The scorer is deterministic, so a pair a person turned down would otherwise
        come back on every run - and a queue that re-asks answered questions stops
        being read."""
        left = repo.create(db, user_id=user.id, device_type=DeviceType.RING, brand="Oura")
        right = repo.create(db, user_id=user.id, device_type=DeviceType.RING, brand="Oura")

        proposal = repo.upsert_proposal(db, user.id, left.id, right.id, score=88.0)
        assert proposal is not None
        repo.decide_proposal(db, proposal, accepted=False, actor="dial@osu.edu")

        assert repo.upsert_proposal(db, user.id, left.id, right.id, score=91.0) is None
        assert repo.pending_proposals(db, user.id) == []

    def test_pair_order_does_not_create_a_duplicate(self, db: Session, user: User, repo: DeviceRepository) -> None:
        left = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        right = repo.create(db, user_id=user.id, device_type=DeviceType.RING)

        repo.upsert_proposal(db, user.id, left.id, right.id, score=70.0)
        repo.upsert_proposal(db, user.id, right.id, left.id, score=75.0)

        assert len(repo.pending_proposals(db, user.id)) == 1

    def test_score_is_stored_at_the_precision_it_is_shown(
        self, db: Session, user: User, repo: DeviceRepository
    ) -> None:
        left = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        right = repo.create(db, user_id=user.id, device_type=DeviceType.RING)
        proposal = repo.upsert_proposal(db, user.id, left.id, right.id, score=87.666)
        assert str(proposal.score) == "87.67"


class TestAttributionLock:
    """Unlinking by hand has to outlive the next sync.

    Detection treats attribution as write-once, but only for a source that already has
    a device: a NULL device_id otherwise reads as "never attributed", so the next batch
    re-attached exactly what a person had just detached. The unlink survived until the
    following sync and then quietly undid itself, which is indistinguishable from the
    button not working.
    """

    @staticmethod
    def _service() -> DeviceService:
        return DeviceService(getLogger(__name__))

    def test_unlinking_records_that_the_empty_state_is_deliberate(self, db: Session, user: User) -> None:
        source = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        device_id = source.device_id
        assert device_id is not None

        self._service().unlink_data_source(db, user.id, device_id, source.id, reason=None, actor="dial@osu.edu")

        db.refresh(source)
        assert source.device_id is None
        assert source.attribution_locked_at is not None

    def test_linking_again_clears_the_lock(self, db: Session, user: User) -> None:
        source = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        device_id = source.device_id
        service = self._service()

        service.unlink_data_source(db, user.id, device_id, source.id, reason=None, actor="dial@osu.edu")
        service.link_data_source(db, user.id, device_id, source.id, reason="put it back", actor="dial@osu.edu")

        db.refresh(source)
        assert source.device_id == device_id
        assert source.attribution_locked_at is None

    def test_a_relink_is_visible_in_the_audit_trail(self, db: Session, user: User) -> None:
        source = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        device_id = source.device_id
        service = self._service()

        service.unlink_data_source(db, user.id, device_id, source.id, reason=None, actor="dial@osu.edu")
        service.link_data_source(db, user.id, device_id, source.id, reason="accidental unlink", actor="dial@osu.edu")

        # Not asserted in order: created_at is the transaction timestamp, so rows
        # written in one test transaction tie and sort arbitrarily. What matters is
        # that both halves of the round trip left a record.
        actions = [e.action for e in db.query(DeviceHistory).filter(DeviceHistory.data_source_id == source.id).all()]
        assert actions.count(DeviceHistoryAction.UNLINKED.value) == 1
        assert actions.count(DeviceHistoryAction.LINKED.value) == 2  # detection, then the relink
        assert all(e.actor for e in db.query(DeviceHistory).filter(DeviceHistory.data_source_id == source.id).all())
