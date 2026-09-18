"""Device registry operations exposed to the API.

Thin over DeviceRepository: its job is to resolve ids to rows, refuse anything that
crosses a user boundary, commit, and shape responses. The rules about what may group
with what live in the repository and in app/services/devices/detection.py.

Every mutating method takes an ``actor`` and threads it into the audit trail. That is
the point of the trail - "the label changed" is not useful six months later, "the
label changed, by this person, for this reason" is.
"""

from datetime import UTC, datetime
from logging import Logger
from uuid import UUID

from app.database import DbSession
from app.models import DataSource, Device
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import LabelSource
from app.schemas.model_crud.devices import (
    DeviceCreate,
    DeviceDataSourceResponse,
    DeviceHistoryListResponse,
    DeviceHistoryResponse,
    DeviceIdentityResponse,
    DeviceListResponse,
    DeviceResponse,
    DeviceUpdate,
    LinkProposalListResponse,
    LinkProposalResponse,
)
from app.services.devices.detection import DeviceDetectionService
from app.utils.exceptions import ResourceNotFoundError, handle_exceptions


class DeviceService:
    def __init__(self, log: Logger):
        self.logger = log
        self.repo = DeviceRepository()
        self.detector = DeviceDetectionService(self.repo)

    # --- reads -------------------------------------------------------------------

    @handle_exceptions
    def list_devices(self, db: DbSession, user_id: UUID, include_retired: bool = True) -> DeviceListResponse:
        devices = self.repo.list_for_user(db, user_id, include_retired=include_retired)
        items = [self._to_response(db, device) for device in devices]
        return DeviceListResponse(items=items, total=len(items))

    @handle_exceptions
    def get_device(self, db: DbSession, user_id: UUID, device_id: UUID) -> DeviceResponse:
        return self._to_response(db, self._owned(db, user_id, device_id))

    @handle_exceptions
    def get_history(
        self,
        db: DbSession,
        user_id: UUID,
        device_id: UUID | None = None,
        data_source_id: UUID | None = None,
        limit: int = 200,
    ) -> DeviceHistoryListResponse:
        """The audit trail, newest first.

        Scoped to the user rather than to the device so that a merged-away device's
        entries still surface: their device_id was nulled when the row was deleted,
        and those are exactly the entries you need to reconstruct what happened.
        """
        if device_id is not None:
            self._owned(db, user_id, device_id)
        entries = self.repo.history_for_user(db, user_id, device_id, data_source_id, limit)
        items = [DeviceHistoryResponse.model_validate(e) for e in entries]
        return DeviceHistoryListResponse(items=items, total=len(items))

    @handle_exceptions
    def list_proposals(self, db: DbSession, user_id: UUID) -> LinkProposalListResponse:
        proposals = self.repo.pending_proposals(db, user_id)
        items = [LinkProposalResponse.model_validate(p) for p in proposals]
        return LinkProposalListResponse(items=items, total=len(items))

    # --- writes ------------------------------------------------------------------

    @handle_exceptions
    def create_device(self, db: DbSession, user_id: UUID, payload: DeviceCreate, actor: str) -> DeviceResponse:
        device = self.repo.create(
            db,
            user_id=user_id,
            device_type=payload.device_type,
            brand=payload.brand,
            model_raw=payload.model_raw,
            model_display=payload.model_display,
            brand_display=payload.brand_display,
            serial=payload.serial,
            firmware_version=payload.firmware_version,
            label=payload.label,
            # Anything a person typed is a manual label from the start, so detection
            # will not later overwrite it.
            label_source=LabelSource.MANUAL if payload.label else LabelSource.AUTO,
            wear_location=payload.wear_location,
            notes=payload.notes,
            actor=actor,
            reason=payload.reason,
        )
        db.commit()
        return self._to_response(db, device)

    @handle_exceptions
    def update_device(
        self,
        db: DbSession,
        user_id: UUID,
        device_id: UUID,
        payload: DeviceUpdate,
        actor: str,
    ) -> DeviceResponse:
        device = self._owned(db, user_id, device_id)
        # exclude_unset, not exclude_none: clearing a label to None is a real edit and
        # has to be distinguishable from not mentioning the field.
        changes = payload.model_dump(exclude_unset=True, exclude={"reason"})
        changes = {k: (v.value if hasattr(v, "value") else v) for k, v in changes.items()}
        if changes:
            self.repo.update_fields(db, device, changes, actor=actor, reason=payload.reason)
            db.commit()
        return self._to_response(db, device)

    @handle_exceptions
    def set_retired(
        self,
        db: DbSession,
        user_id: UUID,
        device_id: UUID,
        retired: bool,
        effective_at: datetime | None = None,
        reason: str | None = None,
        actor: str = "",
    ) -> DeviceResponse:
        device = self._owned(db, user_id, device_id)
        self.repo.set_retired(db, device, retired=retired, effective_at=effective_at, actor=actor, reason=reason)
        db.commit()
        return self._to_response(db, device)

    @handle_exceptions
    def link_data_source(
        self,
        db: DbSession,
        user_id: UUID,
        device_id: UUID,
        data_source_id: UUID,
        reason: str | None,
        actor: str,
    ) -> DeviceResponse:
        device = self._owned(db, user_id, device_id)
        data_source = db.get(DataSource, data_source_id)
        if data_source is None or data_source.user_id != user_id:
            raise ResourceNotFoundError("data_source", data_source_id)

        self.repo.attach_data_source(db, data_source, device, actor=actor, reason=reason)
        # An explicit link answers the question the lock was holding open, so detection
        # is allowed to speak again for this source.
        data_source.attribution_locked_at = None
        db.commit()
        return self._to_response(db, device)

    @handle_exceptions
    def unlink_data_source(
        self,
        db: DbSession,
        user_id: UUID,
        device_id: UUID,
        data_source_id: UUID,
        reason: str | None,
        actor: str,
    ) -> DeviceResponse:
        device = self._owned(db, user_id, device_id)
        data_source = db.get(DataSource, data_source_id)
        if data_source is None or data_source.user_id != user_id:
            raise ResourceNotFoundError("data_source", data_source_id)
        if data_source.device_id != device.id:
            raise ValueError("that data source is not attributed to this device")

        self.repo.attach_data_source(db, data_source, None, actor=actor, reason=reason)
        # Record that the empty state is deliberate. Without this the next sync
        # re-attaches the source from the same claims that attributed it in the first
        # place, and the detach reads as a feature that does not work.
        data_source.attribution_locked_at = datetime.now(UTC)
        db.commit()
        return self._to_response(db, device)

    @handle_exceptions
    def merge(
        self,
        db: DbSession,
        user_id: UUID,
        device_id: UUID,
        absorb_device_id: UUID,
        reason: str | None,
        actor: str,
    ) -> DeviceResponse:
        keep = self._owned(db, user_id, device_id)
        absorb = self._owned(db, user_id, absorb_device_id)
        self.repo.merge(db, keep=keep, absorb=absorb, actor=actor, reason=reason)
        db.commit()
        return self._to_response(db, keep)

    @handle_exceptions
    def split(
        self,
        db: DbSession,
        user_id: UUID,
        device_id: UUID,
        data_source_ids: list[UUID],
        reason: str | None,
        actor: str,
    ) -> DeviceResponse:
        device = self._owned(db, user_id, device_id)
        new_device = self.repo.split(db, device, data_source_ids, actor=actor, reason=reason)
        db.commit()
        return self._to_response(db, new_device)

    @handle_exceptions
    def refresh_proposals(self, db: DbSession, user_id: UUID, actor: str) -> LinkProposalListResponse:
        self.detector.propose_cross_route_links(db, user_id, actor=actor)
        db.commit()
        return self.list_proposals(db, user_id)

    @handle_exceptions
    def decide_proposal(
        self,
        db: DbSession,
        user_id: UUID,
        proposal_id: UUID,
        accepted: bool,
        reason: str | None,
        actor: str,
    ) -> LinkProposalResponse:
        """Accept a proposal (which merges) or reject it (permanently).

        Accepting folds b into a, so the decision is recorded before the merge runs -
        otherwise the merge's own history would be the only trace, and the evidence
        that justified it would be lost with the absorbed row.
        """
        proposal = next((p for p in self.repo.pending_proposals(db, user_id) if p.id == proposal_id), None)
        if proposal is None:
            raise ResourceNotFoundError("device_link_proposal", proposal_id)

        self.repo.decide_proposal(db, proposal, accepted=accepted, actor=actor)
        # Serialize before the merge. Accepting deletes device_b, and the proposal's
        # FK to it cascades, so the row is gone by the time a response could be built
        # from it. The decision itself survives in device_history, which decide_proposal
        # writes with the score and evidence before this point - that is the durable
        # record, and it is why the evidence is copied there rather than referenced.
        response = LinkProposalResponse.model_validate(proposal)

        if accepted:
            keep = self._owned(db, user_id, proposal.device_a_id)
            absorb = self._owned(db, user_id, proposal.device_b_id)
            self.repo.merge(db, keep=keep, absorb=absorb, actor=actor, reason=reason or "Accepted link proposal")
        db.commit()
        return response

    # --- helpers -----------------------------------------------------------------

    def _owned(self, db: DbSession, user_id: UUID, device_id: UUID) -> Device:
        """Fetch a device, refusing one that belongs to someone else.

        Not-found rather than forbidden on purpose: a distinct error would confirm
        that the id exists on another account.
        """
        device = self.repo.get(db, device_id)
        if device is None or device.user_id != user_id:
            raise ResourceNotFoundError("device", device_id)
        return device

    def _to_response(self, db: DbSession, device: Device) -> DeviceResponse:
        return DeviceResponse(
            id=device.id,
            user_id=device.user_id,
            brand=device.brand,
            model_raw=device.model_raw,
            model_display=device.model_display,
            brand_display=device.brand_display,
            host_model_raw=device.host_model_raw,
            serial=device.serial,
            firmware_version=device.firmware_version,
            device_type=device.device_type,
            label=device.label,
            label_source=device.label_source,
            wear_location=device.wear_location,
            notes=device.notes,
            is_active=device.is_active,
            retired_at=device.retired_at,
            first_seen_at=device.first_seen_at,
            last_seen_at=device.last_seen_at,
            created_at=device.created_at,
            updated_at=device.updated_at,
            identities=[DeviceIdentityResponse.model_validate(i) for i in self.repo.claims_for_device(db, device.id)],
            data_sources=[
                DeviceDataSourceResponse(
                    id=ds.id,
                    provider=getattr(ds.provider, "value", ds.provider),
                    source=ds.source,
                    device_model=ds.device_model,
                    device_type=ds.device_type,
                    original_source_name=ds.original_source_name,
                )
                for ds in self.repo.data_sources_for_device(db, device.id)
            ],
        )
