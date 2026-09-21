"""Device registry endpoints: inspect, edit by hand, and read the audit trail.

The registry auto-detects conservatively and deliberately leaves the ambiguous cases
alone, so hand editing is not a fallback here - it is how the ambiguous cases get
resolved. Every mutation is recorded with who made it and why.
"""

from logging import getLogger
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, Query, status

from app.database import DbSession
from app.models import Developer
from app.schemas.model_crud.devices import (
    DeviceCreate,
    DeviceHistoryListResponse,
    DeviceLinkRequest,
    DeviceListResponse,
    DeviceMergeRequest,
    DeviceResponse,
    DeviceRetireRequest,
    DeviceSplitRequest,
    DeviceUpdate,
    LinkProposalDecision,
    LinkProposalListResponse,
    LinkProposalResponse,
)
from app.schemas.model_crud.devices.activity import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    MIN_WINDOW_DAYS,
    SourceActivityListResponse,
)
from app.services import DeveloperDep
from app.services.device_service import DeviceService
from app.services.source_activity_service import SourceActivityService

router = APIRouter()
device_service = DeviceService(log=getLogger(__name__))
source_activity_service = SourceActivityService(log=getLogger(__name__))


def _actor(developer: Developer) -> str:
    """Who to record against a change. The email is the useful handle in an audit log."""
    return developer.email or f"developer:{developer.id}"


@router.get("/users/{user_id}/devices", summary="List a user's devices")
def list_devices(
    db: DbSession,
    _developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    include_retired: Annotated[bool, Query(description="Include devices that are no longer worn.")] = True,
) -> DeviceListResponse:
    return device_service.list_devices(db, user_id, include_retired=include_retired)


@router.post(
    "/users/{user_id}/devices",
    summary="Create a device by hand",
    status_code=status.HTTP_201_CREATED,
)
def create_device(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    payload: DeviceCreate,
) -> DeviceResponse:
    """For hardware no provider has reported yet, or one detection cannot name.

    A label given here is `manual` from the start, so later detection will not
    overwrite it.
    """
    return device_service.create_device(db, user_id, payload, actor=_actor(developer))


@router.get(
    # Ahead of /devices/{device_id}: a literal segment declared after a path parameter
    # is never reached, because the parameter matches it first.
    "/users/{user_id}/devices/source-activity",
    summary="What each of a user's data sources reported recently",
)
def source_activity(
    db: DbSession,
    _developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    days: Annotated[
        int,
        Query(
            ge=MIN_WINDOW_DAYS,
            le=MAX_WINDOW_DAYS,
            description="How far back to count. Clamped; the counts are aggregates over the largest tables.",
        ),
    ] = DEFAULT_WINDOW_DAYS,
) -> SourceActivityListResponse:
    """Evidence for deciding what an unidentified device is, and whose account it is on.

    A source named "Bluetooth Device" or a bare bundle id identifies nothing. What it
    reported does: nights of sleep and a morning readiness score describe a ring or a
    band, a run of cycling workouts describes a head unit, and a source that has gone
    quiet describes a device that came off.

    The account travels with each row - label, classification and login e-mail -
    because for a participant wearing two of one brand that is the other half of the
    same question.
    """
    return source_activity_service.get_user_source_activity(db, user_id, days=days)


@router.get("/users/{user_id}/devices/{device_id}", summary="Get one device")
def get_device(
    db: DbSession,
    _developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID, Path(description="Device ID")],
) -> DeviceResponse:
    return device_service.get_device(db, user_id, device_id)


@router.patch("/users/{user_id}/devices/{device_id}", summary="Edit a device")
def update_device(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID, Path(description="Device ID")],
    payload: DeviceUpdate,
) -> DeviceResponse:
    """Edit the fields that are our interpretation of the hardware.

    `brand` and `model_raw` are not editable: they record what the provider claimed,
    and overwriting them would erase the only evidence of it. Fix the presentation
    with `model_display`.
    """
    return device_service.update_device(db, user_id, device_id, payload, actor=_actor(developer))


@router.post("/users/{user_id}/devices/{device_id}/retire", summary="Retire or reactivate a device")
def retire_device(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID, Path(description="Device ID")],
    payload: DeviceRetireRequest,
) -> DeviceResponse:
    """Record that a unit stopped being worn, with the date it happened.

    Set `effective_at` for a replacement: an identical replacement reports the same
    brand, model and routes as the unit it replaced, so without the date pre- and
    post-swap samples pool into one device and the swap disappears from the analysis.
    """
    return device_service.set_retired(
        db,
        user_id,
        device_id,
        retired=payload.retired,
        effective_at=payload.effective_at,
        reason=payload.reason,
        actor=_actor(developer),
    )


@router.post("/users/{user_id}/devices/{device_id}/link", summary="Attribute a data source to a device")
def link_data_source(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID, Path(description="Device ID")],
    payload: DeviceLinkRequest,
) -> DeviceResponse:
    """Attribution groups data sources; it never merges them.

    The same ring read from its maker's API and relayed through Apple Health stays
    two data sources under one device, because the relay changes freshness, rounding
    and completeness and that comparison has to remain available.
    """
    return device_service.link_data_source(
        db, user_id, device_id, payload.data_source_id, payload.reason, actor=_actor(developer)
    )


@router.post("/users/{user_id}/devices/{device_id}/unlink", summary="Detach a data source from a device")
def unlink_data_source(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID, Path(description="Device ID")],
    payload: DeviceLinkRequest,
) -> DeviceResponse:
    try:
        return device_service.unlink_data_source(
            db, user_id, device_id, payload.data_source_id, payload.reason, actor=_actor(developer)
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/users/{user_id}/devices/{device_id}/merge", summary="Merge another device into this one")
def merge_devices(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID, Path(description="Device ID to keep")],
    payload: DeviceMergeRequest,
) -> DeviceResponse:
    """Irreversible: the absorbed device row is deleted.

    Everything needed to rebuild it by hand is written to history first. Prefer
    accepting a link proposal where one exists, so the evidence is recorded with the
    decision.
    """
    try:
        return device_service.merge(
            db, user_id, device_id, payload.absorb_device_id, payload.reason, actor=_actor(developer)
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/users/{user_id}/devices/{device_id}/split", summary="Split data sources onto a new device")
def split_device(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID, Path(description="Device ID to split")],
    payload: DeviceSplitRequest,
) -> DeviceResponse:
    """Returns the newly created device. Splitting every source off is refused."""
    try:
        return device_service.split(
            db, user_id, device_id, payload.data_source_ids, payload.reason, actor=_actor(developer)
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/users/{user_id}/devices-history", summary="Device attribution history")
def get_device_history(
    db: DbSession,
    _developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    device_id: Annotated[UUID | None, Query(description="Only this device's entries.")] = None,
    data_source_id: Annotated[UUID | None, Query(description="Only this data source's entries.")] = None,
    limit: Annotated[int, Query(ge=1, le=1000, description="Max entries, newest first.")] = 200,
) -> DeviceHistoryListResponse:
    """Append-only record of every attribution change, newest first.

    Scoped to the user rather than the device, so entries for a device that has since
    been merged away still appear - those are exactly the ones needed to work out what
    happened to data that moved.
    """
    return device_service.get_history(db, user_id, device_id, data_source_id, limit)


@router.get("/users/{user_id}/device-link-proposals", summary="Pending cross-route link proposals")
def list_link_proposals(
    db: DbSession,
    _developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
) -> LinkProposalListResponse:
    """Pairs of devices that may be one unit seen by different ingest routes.

    No identifier is shared across routes, so these are inferred from temporal
    overlap of the same measurement and are never applied automatically. `evidence`
    carries what produced the score.
    """
    return device_service.list_proposals(db, user_id)


@router.post("/users/{user_id}/device-link-proposals/refresh", summary="Re-score cross-route link proposals")
def refresh_link_proposals(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
) -> LinkProposalListResponse:
    """Re-run the scorer. Pairs a person already rejected are not revived."""
    return device_service.refresh_proposals(db, user_id, actor=_actor(developer))


@router.post(
    "/users/{user_id}/device-link-proposals/{proposal_id}/decide",
    summary="Accept or reject a link proposal",
)
def decide_link_proposal(
    db: DbSession,
    developer: DeveloperDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    proposal_id: Annotated[UUID, Path(description="Proposal ID")],
    payload: LinkProposalDecision,
) -> LinkProposalResponse:
    """Accepting merges device_b into device_a. Rejecting is permanent.

    A rejection is kept forever so the deterministic scorer cannot re-raise a
    question that has been answered - a queue that keeps re-asking stops being read.
    """
    try:
        return device_service.decide_proposal(
            db, user_id, proposal_id, payload.accepted, payload.reason, actor=_actor(developer)
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
