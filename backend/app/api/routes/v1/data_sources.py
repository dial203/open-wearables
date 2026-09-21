"""API endpoints for user data sources."""

from logging import getLogger
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Path, status

from app.database import DbSession
from app.schemas.model_crud.data_priority import DataSourceListResponse, DataSourceRelayUpdate, DataSourceResponse
from app.services import ApiKeyDep, PriorityService

router = APIRouter()
priority_service = PriorityService(log=getLogger(__name__))


@router.get(
    "/users/{user_id}/data-sources",
    summary="Get user data sources",
)
def get_user_data_sources(
    db: DbSession,
    _api_key: ApiKeyDep,
    user_id: Annotated[UUID, Path(description="User ID")],
) -> DataSourceListResponse:
    return priority_service.get_user_data_sources(db, user_id)


@router.patch(
    # Its own path rather than a general data-source PATCH: this sets one field with one
    # meaning, and a generic update endpoint here would invite callers to write over the
    # ingest fingerprint (provider, device_model, source), which is the record of what a
    # provider actually reported.
    "/users/{user_id}/data-sources/{data_source_id}/relay-visibility",
    summary="Set whether a relayed source is hidden when its maker is connected directly",
)
def set_relay_visibility(
    db: DbSession,
    _api_key: ApiKeyDep,
    user_id: Annotated[UUID, Path(description="User ID")],
    data_source_id: Annotated[UUID, Path(description="Data source ID")],
    payload: DataSourceRelayUpdate,
) -> DataSourceResponse:
    """Override the redundant-relay rule for one source.

    Reads leave out an aggregator's copy of a maker that is also connected directly,
    for the span the direct route covers. `always` exempts this source from that -
    for a relay carrying something the maker's own API does not - and `never` hides
    it outright. No setting deletes anything: `include_redundant_relays=true` still
    returns every row.
    """
    updated = priority_service.set_relay_visibility(db, user_id, data_source_id, payload.relay_visibility)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")
    return updated
