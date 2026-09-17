"""Persistence for the device registry, with the audit trail written alongside.

Every mutation here writes a ``device_history`` row in the same flush as the change
it describes. That is deliberate rather than a convenience: device attribution
decides which physical unit a sample is credited to, so a change nobody recorded is
a silent rewrite of the provenance of historical data. Callers cannot mutate a
device without going through a method that records it.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select

from app.database import DbSession
from app.models import DataSource, Device, DeviceHistory, DeviceIdentity, DeviceLinkProposal
from app.schemas.enums import (
    STRONG_IDENTITY_KINDS,
    DeviceHistoryAction,
    DeviceIdentityKind,
    DeviceType,
    LabelSource,
    LinkProposalStatus,
)
from app.services.devices.identity import IdentityClaim

SYSTEM_ACTOR = "system:detection"


class DeviceRepository:
    # --- history -----------------------------------------------------------------

    def record(
        self,
        db_session: DbSession,
        user_id: UUID,
        action: DeviceHistoryAction,
        device_id: UUID | None = None,
        data_source_id: UUID | None = None,
        field: str | None = None,
        old_value: str | None = None,
        new_value: str | None = None,
        actor: str | None = None,
        reason: str | None = None,
        meta: dict | None = None,
    ) -> DeviceHistory:
        entry = DeviceHistory(
            id=uuid4(),
            user_id=user_id,
            device_id=device_id,
            data_source_id=data_source_id,
            action=action.value,
            field=field,
            old_value=old_value,
            new_value=new_value,
            actor=actor or SYSTEM_ACTOR,
            reason=reason,
            meta=meta,
        )
        db_session.add(entry)
        return entry

    def history_for_user(
        self,
        db_session: DbSession,
        user_id: UUID,
        device_id: UUID | None = None,
        data_source_id: UUID | None = None,
        limit: int = 200,
    ) -> list[DeviceHistory]:
        stmt = select(DeviceHistory).where(DeviceHistory.user_id == user_id)
        if device_id:
            stmt = stmt.where(DeviceHistory.device_id == device_id)
        if data_source_id:
            stmt = stmt.where(DeviceHistory.data_source_id == data_source_id)
        stmt = stmt.order_by(DeviceHistory.created_at.desc()).limit(limit)
        return list(db_session.scalars(stmt).all())

    # --- devices -----------------------------------------------------------------

    def get(self, db_session: DbSession, device_id: UUID) -> Device | None:
        return db_session.get(Device, device_id)

    def list_for_user(self, db_session: DbSession, user_id: UUID, include_retired: bool = True) -> list[Device]:
        stmt = select(Device).where(Device.user_id == user_id)
        if not include_retired:
            stmt = stmt.where(Device.is_active.is_(True))
        return list(db_session.scalars(stmt.order_by(Device.brand, Device.model_raw, Device.created_at)).all())

    def create(
        self,
        db_session: DbSession,
        user_id: UUID,
        device_type: DeviceType | str = DeviceType.UNKNOWN,
        brand: str | None = None,
        model_raw: str | None = None,
        model_display: str | None = None,
        label: str | None = None,
        label_source: LabelSource = LabelSource.AUTO,
        wear_location: str | None = None,
        notes: str | None = None,
        actor: str | None = None,
        reason: str | None = None,
        detected: bool = False,
    ) -> Device:
        now = datetime.now(UTC)
        device = Device(
            id=uuid4(),
            user_id=user_id,
            brand=brand,
            model_raw=model_raw,
            model_display=model_display,
            device_type=getattr(device_type, "value", device_type),
            label=label,
            label_source=label_source.value,
            wear_location=wear_location,
            notes=notes,
            is_active=True,
            first_seen_at=now,
            last_seen_at=now,
            updated_at=now,
        )
        db_session.add(device)
        db_session.flush()
        self.record(
            db_session,
            user_id=user_id,
            device_id=device.id,
            action=DeviceHistoryAction.DETECTED if detected else DeviceHistoryAction.CREATED,
            actor=actor,
            reason=reason,
            meta={"brand": brand, "model_raw": model_raw, "device_type": device.device_type},
        )
        return device

    # Fields a person may edit. device_type is here and brand/model_raw are not:
    # brand and model_raw are the record of what the provider reported, and editing
    # them in place would erase the only evidence of what the hardware actually
    # claimed to be. A misread type is our inference and is ours to correct.
    EDITABLE_FIELDS = ("label", "device_type", "wear_location", "notes", "model_display")

    def update_fields(
        self,
        db_session: DbSession,
        device: Device,
        changes: dict[str, str | None],
        actor: str | None = None,
        reason: str | None = None,
    ) -> Device:
        """Apply hand edits, one history row per field that actually changed."""
        for field, new_value in changes.items():
            if field not in self.EDITABLE_FIELDS:
                raise ValueError(f"{field} is not editable")
            old_value = getattr(device, field)
            if old_value == new_value:
                continue
            setattr(device, field, new_value)
            self.record(
                db_session,
                user_id=device.user_id,
                device_id=device.id,
                action=DeviceHistoryAction.UPDATED,
                field=field,
                old_value=None if old_value is None else str(old_value),
                new_value=None if new_value is None else str(new_value),
                actor=actor,
                reason=reason,
            )
            # A hand-set label pins the device: detection must never overwrite it.
            # A confidently wrong auto-label is worse than no label, because nothing
            # about it signals that it is wrong.
            if field == "label":
                device.label_source = LabelSource.MANUAL.value if new_value else LabelSource.AUTO.value

        device.updated_at = datetime.now(UTC)
        db_session.flush()
        return device

    def set_retired(
        self,
        db_session: DbSession,
        device: Device,
        retired: bool,
        effective_at: datetime | None = None,
        actor: str | None = None,
        reason: str | None = None,
    ) -> Device:
        """Retire or reactivate a device, with the date it stopped being worn.

        The date matters more than the flag. A warranty replacement reports the same
        brand, model and routes as the unit it replaced, so nothing in the provider
        data distinguishes them - without an effective date, pre- and post-swap
        samples pool into one device and the swap becomes invisible in analysis.
        """
        device.is_active = not retired
        device.retired_at = (effective_at or datetime.now(UTC)) if retired else None
        device.updated_at = datetime.now(UTC)
        self.record(
            db_session,
            user_id=device.user_id,
            device_id=device.id,
            action=DeviceHistoryAction.RETIRED if retired else DeviceHistoryAction.REACTIVATED,
            field="retired_at",
            new_value=device.retired_at.isoformat() if device.retired_at else None,
            actor=actor,
            reason=reason,
        )
        db_session.flush()
        return device

    def touch_seen(self, db_session: DbSession, device: Device) -> None:
        """Bump last_seen_at. No history row: this is traffic, not a decision."""
        now = datetime.now(UTC)
        device.last_seen_at = now
        if device.first_seen_at is None:
            device.first_seen_at = now

    # --- identity claims ---------------------------------------------------------

    def find_by_claim(self, db_session: DbSession, user_id: UUID, claim: IdentityClaim) -> Device | None:
        stmt = (
            select(Device)
            .join(DeviceIdentity, DeviceIdentity.device_id == Device.id)
            .where(
                DeviceIdentity.user_id == user_id,
                DeviceIdentity.route == claim.route,
                DeviceIdentity.id_kind == claim.kind.value,
                DeviceIdentity.id_value == claim.value,
            )
        )
        return db_session.scalars(stmt).first()

    def add_claim(
        self,
        db_session: DbSession,
        device: Device,
        claim: IdentityClaim,
        actor: str | None = None,
    ) -> DeviceIdentity | None:
        """Attach a claim to a device, or return None if another device already owns it.

        A claim is never moved from one device to another here. If two devices each
        hold a claim that now points at the same unit, that is a link question - and
        answering it by silently repointing rows would merge them with no record and
        no review. The caller turns the refusal into a proposal instead.
        """
        existing = (
            db_session.query(DeviceIdentity)
            .filter(
                DeviceIdentity.user_id == device.user_id,
                DeviceIdentity.route == claim.route,
                DeviceIdentity.id_kind == claim.kind.value,
                DeviceIdentity.id_value == claim.value,
            )
            .one_or_none()
        )
        now = datetime.now(UTC)
        if existing is not None:
            if existing.device_id != device.id:
                return None
            existing.last_seen_at = now
            return existing

        identity = DeviceIdentity(
            id=uuid4(),
            user_id=device.user_id,
            device_id=device.id,
            route=claim.route,
            id_kind=claim.kind.value,
            id_value=claim.value,
            confidence=claim.confidence.value,
            first_seen_at=now,
            last_seen_at=now,
        )
        db_session.add(identity)
        db_session.flush()
        self.record(
            db_session,
            user_id=device.user_id,
            device_id=device.id,
            action=DeviceHistoryAction.IDENTITY_ADDED,
            field=claim.kind.value,
            new_value=claim.value,
            actor=actor,
            meta={"route": claim.route, "confidence": claim.confidence.value},
        )
        return identity

    def claims_for_device(self, db_session: DbSession, device_id: UUID) -> list[DeviceIdentity]:
        stmt = select(DeviceIdentity).where(DeviceIdentity.device_id == device_id)
        return list(db_session.scalars(stmt.order_by(DeviceIdentity.route, DeviceIdentity.id_kind)).all())

    def strong_claim_kinds(self) -> frozenset[DeviceIdentityKind]:
        return STRONG_IDENTITY_KINDS

    # --- data source attribution -------------------------------------------------

    def attach_data_source(
        self,
        db_session: DbSession,
        data_source: DataSource,
        device: Device | None,
        actor: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Point a data source at a device, or clear it. Never merges the rows.

        Attribution groups data sources; it does not combine them. The same ring read
        from Oura's API and relayed through Apple Health differs in freshness,
        rounding and completeness, so "this ring, direct vs via Apple Health" has to
        remain a comparison a consumer can run.
        """
        previous = data_source.device_id
        new_id = device.id if device else None
        if previous == new_id:
            return

        data_source.device_id = new_id
        self.record(
            db_session,
            user_id=data_source.user_id,
            device_id=new_id or previous,
            data_source_id=data_source.id,
            action=DeviceHistoryAction.LINKED if device else DeviceHistoryAction.UNLINKED,
            field="device_id",
            old_value=str(previous) if previous else None,
            new_value=str(new_id) if new_id else None,
            actor=actor,
            reason=reason,
        )
        db_session.flush()

    def data_sources_for_device(self, db_session: DbSession, device_id: UUID) -> list[DataSource]:
        stmt = select(DataSource).where(DataSource.device_id == device_id)
        return list(db_session.scalars(stmt.order_by(DataSource.provider, DataSource.source)).all())

    # --- merge / split -----------------------------------------------------------

    def merge(
        self,
        db_session: DbSession,
        keep: Device,
        absorb: Device,
        actor: str | None = None,
        reason: str | None = None,
    ) -> Device:
        """Fold ``absorb`` into ``keep``: its data sources and claims move, then it goes.

        Merging is the irreversible direction, which is why nothing calls it
        automatically. Everything needed to undo it by hand is written to history
        first - the absorbed device's full field set and the ids of every data source
        and claim that moved - because the row itself will not survive the call.

        The data sources are re-pointed, never combined. Two sources that described
        one unit by different routes still differ in freshness, rounding and
        completeness, and collapsing them would destroy the comparison.
        """
        if keep.id == absorb.id:
            raise ValueError("cannot merge a device into itself")
        if keep.user_id != absorb.user_id:
            raise ValueError("cannot merge devices belonging to different users")

        moved_sources = self.data_sources_for_device(db_session, absorb.id)
        moved_claims = self.claims_for_device(db_session, absorb.id)

        self.record(
            db_session,
            user_id=keep.user_id,
            device_id=keep.id,
            action=DeviceHistoryAction.MERGED,
            old_value=str(absorb.id),
            new_value=str(keep.id),
            actor=actor,
            reason=reason,
            meta={
                # The absorbed row is about to be deleted and its FK in this history
                # entry will null out, so its identity is kept here as plain values.
                "absorbed": {
                    "id": str(absorb.id),
                    "brand": absorb.brand,
                    "model_raw": absorb.model_raw,
                    "model_display": absorb.model_display,
                    "device_type": absorb.device_type,
                    "label": absorb.label,
                    "label_source": absorb.label_source,
                    "wear_location": absorb.wear_location,
                    "notes": absorb.notes,
                    "is_active": absorb.is_active,
                    "retired_at": absorb.retired_at.isoformat() if absorb.retired_at else None,
                    "first_seen_at": absorb.first_seen_at.isoformat() if absorb.first_seen_at else None,
                    "last_seen_at": absorb.last_seen_at.isoformat() if absorb.last_seen_at else None,
                },
                "moved_data_source_ids": [str(ds.id) for ds in moved_sources],
                "moved_claims": [
                    {"route": c.route, "id_kind": c.id_kind, "id_value": c.id_value, "confidence": c.confidence}
                    for c in moved_claims
                ],
            },
        )

        for data_source in moved_sources:
            data_source.device_id = keep.id
        for claim in moved_claims:
            claim.device_id = keep.id

        # Keep the earliest first_seen and the latest last_seen: the merged device has
        # been in service for the union of the two spans, not just the survivor's.
        keep.first_seen_at = _earliest(keep.first_seen_at, absorb.first_seen_at)
        keep.last_seen_at = _latest(keep.last_seen_at, absorb.last_seen_at)
        # An auto label never wins over a hand-set one.
        if (
            absorb.label
            and keep.label_source == LabelSource.AUTO.value
            and absorb.label_source == LabelSource.MANUAL.value
        ):
            keep.label = absorb.label
            keep.label_source = LabelSource.MANUAL.value
        keep.updated_at = datetime.now(UTC)

        db_session.flush()
        db_session.delete(absorb)
        db_session.flush()
        return keep

    def split(
        self,
        db_session: DbSession,
        device: Device,
        data_source_ids: list[UUID],
        actor: str | None = None,
        reason: str | None = None,
    ) -> Device:
        """Move some of a device's data sources onto a new device.

        The undo for a merge that turned out to be wrong, and the fix for a detection
        that grouped too eagerly. The new device inherits the descriptive fields but
        not the label: whatever the old label named, it named the thing that stayed.
        """
        if not data_source_ids:
            raise ValueError("split needs at least one data source to move")

        moving = [ds for ds in self.data_sources_for_device(db_session, device.id) if ds.id in set(data_source_ids)]
        if not moving:
            raise ValueError("none of those data sources belong to this device")
        if len(moving) == len(self.data_sources_for_device(db_session, device.id)):
            raise ValueError("splitting every data source off would leave an empty device; rename it instead")

        new_device = self.create(
            db_session,
            user_id=device.user_id,
            device_type=device.device_type,
            brand=device.brand,
            model_raw=device.model_raw,
            model_display=device.model_display,
            actor=actor,
            reason=reason or f"Split from device {device.id}",
        )

        for data_source in moving:
            data_source.device_id = new_device.id

        # Claims follow their data source's route, so a route that left entirely takes
        # its claims with it. A route still represented on the original device keeps
        # them: the claim is about that route's view, not about the split.
        remaining_routes = {
            getattr(ds.provider, "value", ds.provider) for ds in self.data_sources_for_device(db_session, device.id)
        }
        moved_routes = {getattr(ds.provider, "value", ds.provider) for ds in moving}
        for claim in self.claims_for_device(db_session, device.id):
            if claim.route in moved_routes and claim.route not in remaining_routes:
                claim.device_id = new_device.id

        self.record(
            db_session,
            user_id=device.user_id,
            device_id=device.id,
            action=DeviceHistoryAction.SPLIT,
            old_value=str(device.id),
            new_value=str(new_device.id),
            actor=actor,
            reason=reason,
            meta={"moved_data_source_ids": [str(ds.id) for ds in moving]},
        )
        device.updated_at = datetime.now(UTC)
        db_session.flush()
        return new_device

    # --- link proposals ----------------------------------------------------------

    @staticmethod
    def _ordered(a: UUID, b: UUID) -> tuple[UUID, UUID]:
        """Canonical pair order, matching the table's CHECK constraint."""
        return (a, b) if str(a) < str(b) else (b, a)

    def get_proposal(self, db_session: DbSession, device_a: UUID, device_b: UUID) -> DeviceLinkProposal | None:
        first, second = self._ordered(device_a, device_b)
        return (
            db_session.query(DeviceLinkProposal)
            .filter(DeviceLinkProposal.device_a_id == first, DeviceLinkProposal.device_b_id == second)
            .one_or_none()
        )

    def upsert_proposal(
        self,
        db_session: DbSession,
        user_id: UUID,
        device_a: UUID,
        device_b: UUID,
        score: float,
        evidence: dict | None = None,
        actor: str | None = None,
    ) -> DeviceLinkProposal | None:
        """Record a candidate link, or return None if this pair was already decided.

        A rejected proposal is never revived. The scorer is deterministic, so a pair a
        person has already turned down would otherwise come back on every run, and a
        queue that keeps re-asking the same question stops being read.
        """
        first, second = self._ordered(device_a, device_b)
        existing = self.get_proposal(db_session, first, second)
        now = datetime.now(UTC)
        # The column is NUMERIC(5,2); quantize here rather than letting the driver
        # round, so the score a reviewer sees is the score that was stored.
        stored_score = Decimal(str(score)).quantize(Decimal("0.01"))

        if existing is not None:
            if existing.status != LinkProposalStatus.PENDING.value:
                return None
            existing.score = stored_score
            existing.evidence = evidence
            existing.updated_at = now
            return existing

        proposal = DeviceLinkProposal(
            id=uuid4(),
            user_id=user_id,
            device_a_id=first,
            device_b_id=second,
            score=stored_score,
            evidence=evidence,
            status=LinkProposalStatus.PENDING.value,
            updated_at=now,
        )
        db_session.add(proposal)
        db_session.flush()
        self.record(
            db_session,
            user_id=user_id,
            device_id=first,
            action=DeviceHistoryAction.LINK_PROPOSED,
            new_value=str(second),
            actor=actor,
            meta={"score": float(stored_score), "evidence": evidence},
        )
        return proposal

    def pending_proposals(self, db_session: DbSession, user_id: UUID) -> list[DeviceLinkProposal]:
        stmt = (
            select(DeviceLinkProposal)
            .where(
                DeviceLinkProposal.user_id == user_id,
                DeviceLinkProposal.status == LinkProposalStatus.PENDING.value,
            )
            .order_by(DeviceLinkProposal.score.desc())
        )
        return list(db_session.scalars(stmt).all())

    def decide_proposal(
        self,
        db_session: DbSession,
        proposal: DeviceLinkProposal,
        accepted: bool,
        actor: str | None = None,
    ) -> DeviceLinkProposal:
        proposal.status = (LinkProposalStatus.ACCEPTED if accepted else LinkProposalStatus.REJECTED).value
        proposal.decided_at = datetime.now(UTC)
        proposal.decided_by = actor
        proposal.updated_at = datetime.now(UTC)
        self.record(
            db_session,
            user_id=proposal.user_id,
            device_id=proposal.device_a_id,
            action=DeviceHistoryAction.LINK_ACCEPTED if accepted else DeviceHistoryAction.LINK_REJECTED,
            new_value=str(proposal.device_b_id),
            actor=actor,
            meta={"score": float(proposal.score), "evidence": proposal.evidence},
        )
        db_session.flush()
        return proposal


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    return min((x for x in (a, b) if x is not None), default=None)


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    return max((x for x in (a, b) if x is not None), default=None)
