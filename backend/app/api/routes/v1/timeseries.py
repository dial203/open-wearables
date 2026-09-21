from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.database import DbSession
from app.schemas.enums import ProviderName, Resolution, SeriesType
from app.schemas.model_crud.activities import TimeSeriesQueryParams
from app.schemas.responses.activity import TimeSeriesSample
from app.schemas.utils import PaginatedResponse
from app.services import ApiKeyDep, timeseries_service
from app.utils.dates import DateTimeQueryParam, parse_query_datetime, parse_query_end_datetime
from app.utils.pagination import DEFAULT_PAGE_SIZE, PageLimitQueryParam

router = APIRouter()


@router.get("/users/{user_id}/timeseries")
def get_timeseries(
    user_id: UUID,
    start_time: DateTimeQueryParam,
    end_time: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    types: Annotated[list[SeriesType], Query()] = [],
    resolution: Resolution = Resolution.RAW,
    cursor: str | None = None,
    limit: PageLimitQueryParam = DEFAULT_PAGE_SIZE,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    include_redundant_relays: Annotated[
        bool,
        Query(
            description=(
                "Keep the aggregator's copy of a maker that is also connected directly. Off by "
                "default: data relayed through Apple Health, Google Health or Health Connect is "
                "left out for the span in which the maker's own API delivered the same data, so a "
                "device connected both ways is not counted twice. Nothing is deleted - set this to "
                "read the relayed copies back. `metadata.relay_dedup` lists what was left out."
            ),
        ),
    ] = False,
    filter_by_priority: bool = False,
) -> PaginatedResponse[TimeSeriesSample]:
    """Returns granular time series data (biometrics or activity)."""
    params = TimeSeriesQueryParams(
        start_datetime=parse_query_datetime(start_time),
        end_datetime=parse_query_end_datetime(end_time),
        limit=limit,
        cursor=cursor,
        resolution=resolution,
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        include_redundant_relays=include_redundant_relays,
    )
    return timeseries_service.get_timeseries(db, user_id, types, params, filter_by_priority=filter_by_priority)
