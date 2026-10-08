from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BeforeValidator

from app.api.routes.v1.timeseries import split_comma_types
from app.database import DbSession
from app.schemas.enums import ProviderName, SeriesType, WorkoutType
from app.schemas.model_crud.activities import EventRecordQueryParams, SleepInclude, WorkoutInclude
from app.schemas.responses.activity import (
    Meal,
    MenstrualCycleRecord,
    SleepSession,
    SleepTotals,
    Workout,
    WorkoutTotals,
)
from app.schemas.utils import PaginatedResponse
from app.services import ApiKeyDep
from app.services.event_record_service import event_record_service
from app.services.workout_export_service import MAX_PAD_SECONDS, CsvExport, SampleLayout, workout_export_service
from app.utils.dates import DateTimeQueryParam, parse_query_datetime, parse_query_end_datetime
from app.utils.pagination import DEFAULT_PAGE_SIZE, PageLimitQueryParam

router = APIRouter()

# Shared by each list and the totals that add it up, so the two cannot drift apart.
WorkoutTypeQuery = Annotated[
    WorkoutType | None,
    Query(alias="type", description="Exact normalized workout type. Unlike `record_type`, does not substring-match."),
]
IsNapQuery = Annotated[
    bool | None,
    Query(description="When true, return only naps; when false, only main sleep. Omit to return both."),
]
IncludeRedundantRelaysQuery = Annotated[
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
]
FilterByPriorityQuery = Annotated[
    bool,
    Query(
        description="When true, keep only the highest-priority source's sessions per sleep date "
        "(provider/device priority, same ranking as summaries). Defaults to false for backwards compatibility."
    ),
]


@router.get("/users/{user_id}/events/workouts")
def list_workouts(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    include: Annotated[list[WorkoutInclude], Query(default_factory=list)],
    record_type: str | None = None,
    workout_type: WorkoutTypeQuery = None,
    cursor: str | None = None,
    limit: PageLimitQueryParam = DEFAULT_PAGE_SIZE,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
) -> PaginatedResponse[Workout]:
    """Returns workout sessions."""
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_end_datetime(end_date),
        cursor=cursor,
        limit=limit,
        record_type=record_type,
        workout_type=workout_type,
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        include_redundant_relays=include_redundant_relays,
    )
    return event_record_service.get_workouts(db, user_id, params, include=include)


@router.get("/users/{user_id}/events/workouts/types")
def list_workout_types(
    user_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> list[str]:
    """Returns the workout types this user actually has, for populating a filter."""
    return event_record_service.get_workout_types(db, user_id)


@router.get("/users/{user_id}/events/workouts/totals")
def get_workout_totals(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    record_type: str | None = None,
    workout_type: WorkoutTypeQuery = None,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
) -> WorkoutTotals:
    """Returns the count, duration, energy and distance of the workouts the same filters would list.

    Added up in the database, so it covers every matching workout however many
    there are, without paging through them.
    """
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_end_datetime(end_date),
        record_type=record_type,
        workout_type=workout_type,
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        include_redundant_relays=include_redundant_relays,
    )
    return event_record_service.get_workout_totals(db, user_id, params)


def _csv_response(export: CsvExport) -> Response:
    return Response(
        content=export.content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "X-Export-Row-Count": str(export.row_count),
            "X-Export-Source-Count": str(export.source_count),
        },
    )


@router.get("/users/{user_id}/events/workouts/export")
def export_workouts(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    record_type: str | None = None,
    workout_type: WorkoutTypeQuery = None,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
) -> Response:
    """One CSV row per workout: the same workouts, under the same filters, as the list.

    Every summary metric the list carries, time-in-zone flattened to one column per zone,
    and the full source attribution. `avg_hr_origin` says whether the average came from
    the provider or was computed by OW from the workout's own samples, because the
    provider reported none. `overlap_group` gives workouts that overlap in time the same
    number, which is how the records several devices filed for one session pair up.
    `X-Export-Row-Count` holds the number of workouts.
    """
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_end_datetime(end_date),
        record_type=record_type,
        workout_type=workout_type,
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        include_redundant_relays=include_redundant_relays,
    )
    return _csv_response(workout_export_service.export_workout_summaries(db, user_id, params))


@router.get("/users/{user_id}/events/workouts/{workout_id}/export")
def export_workout_samples(
    user_id: UUID,
    workout_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
    layout: Annotated[
        SampleLayout,
        Query(
            description=(
                "`wide`: one row per second of the workout, one column per source and metric, "
                "so devices worn together line up row by row. A sample goes to the second it "
                "was recorded in (UTC, truncated); an empty cell is a second with no sample. If a "
                "source recorded more than one sample of a metric within a second the cell is "
                "their mean and a `... n` column beside it holds the count. Beat-to-beat series "
                "(`rr_interval`, `pulse_to_pulse_interval`) are not on the grid. "
                "`long`: one row per stored sample, untouched, with its full attribution - the "
                "lossless form, and the only one carrying beat-to-beat intervals."
            ),
        ),
    ] = SampleLayout.WIDE,
    types: Annotated[
        list[SeriesType],
        Query(description="Metrics to export. Omit for every metric recorded in the window."),
        BeforeValidator(split_comma_types),
    ] = [],
    pad_seconds: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_PAD_SECONDS,
            description="Widen the window by this many seconds each side, for a device started early or stopped late.",
        ),
    ] = 0,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
) -> Response:
    """CSV of every sample recorded during a workout, from every device that recorded one.

    The window is the workout's own start and end. Samples are not limited to the device
    that filed the workout: anything worn at the same time is included, each source kept
    apart and named in the header (`wide`) or on every row (`long`). Data is exported at
    the resolution it is stored at - nothing is resampled or interpolated.

    `X-Export-Row-Count` holds the number of samples read and `X-Export-Source-Count` the
    number of sources they came from; both are 0 when nothing was recorded in the window.
    """
    export = workout_export_service.export_workout_samples(
        db,
        user_id,
        workout_id,
        layout=layout,
        types=types,
        pad_seconds=pad_seconds,
        include_redundant_relays=include_redundant_relays,
    )
    if export is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workout not found")
    return _csv_response(export)


@router.get("/users/{user_id}/events/sleep")
def list_sleep_sessions(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    include: Annotated[list[SleepInclude], Query(default_factory=list)],
    cursor: str | None = None,
    limit: PageLimitQueryParam = DEFAULT_PAGE_SIZE,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
    is_nap: IsNapQuery = None,
    filter_by_priority: FilterByPriorityQuery = False,
) -> PaginatedResponse[SleepSession]:
    """Returns sleep sessions (including naps)."""
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_end_datetime(end_date),
        cursor=cursor,
        limit=limit,
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        include_redundant_relays=include_redundant_relays,
        is_nap=is_nap,
    )
    return event_record_service.get_sleep_sessions(
        db, user_id, params, filter_by_priority=filter_by_priority, include=include
    )


@router.get("/users/{user_id}/events/workouts/{workout_id}/fit")
def download_workout_fit(
    user_id: UUID,
    workout_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> Response:
    """Download the raw FIT file stored for a workout.

    Only Garmin delivers raw FIT files, and only when `STORE_FIT_FILES` is enabled on the
    instance. Returns the `.fit` bytes as an attachment. Use the `has_fit_file` flag on the
    workout list/detail responses to know which workouts have a file. Responds 404 when the
    workout doesn't exist for this user or no FIT file is stored for it.
    """
    result = event_record_service.get_workout_fit_file(db, user_id, workout_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No FIT file available for this workout")
    fit_bytes, filename = result
    return Response(
        content=fit_bytes,
        media_type="application/vnd.ant.fit",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/users/{user_id}/events/sleep/totals")
def get_sleep_totals(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    is_nap: IsNapQuery = None,
    filter_by_priority: FilterByPriorityQuery = False,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
) -> SleepTotals:
    """Returns the count, naps, time asleep, time in bed and mean efficiency of the sessions.

    The same filters as the sleep list, so it adds up exactly what that would page through.

    Added up in the database, so it covers every matching session however many
    there are, without paging through them.
    """
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_end_datetime(end_date),
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        is_nap=is_nap,
        include_redundant_relays=include_redundant_relays,
    )
    return event_record_service.get_sleep_totals(db, user_id, params, filter_by_priority=filter_by_priority)


@router.get("/users/{user_id}/events/menstrual-cycles")
def list_menstrual_cycles(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    cursor: str | None = None,
    limit: PageLimitQueryParam = DEFAULT_PAGE_SIZE,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
) -> PaginatedResponse[MenstrualCycleRecord]:
    """Returns menstrual cycle records."""
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_end_datetime(end_date),
        cursor=cursor,
        limit=limit,
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        include_redundant_relays=include_redundant_relays,
    )
    return event_record_service.get_menstrual_cycles(db, user_id, params)


@router.get("/users/{user_id}/events/meals")
def list_meals(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    cursor: str | None = None,
    limit: PageLimitQueryParam = DEFAULT_PAGE_SIZE,
    provider: ProviderName | None = None,
    source: str | None = None,
    device_model: str | None = None,
    data_source_id: UUID | None = None,
    include_redundant_relays: IncludeRedundantRelaysQuery = False,
) -> PaginatedResponse[Meal]:
    """Returns meals (nutrition entries)."""
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_end_datetime(end_date),
        cursor=cursor,
        limit=limit,
        provider=provider,
        source=source,
        device_model=device_model,
        data_source_id=data_source_id,
        include_redundant_relays=include_redundant_relays,
    )
    return event_record_service.get_meals(db, user_id, params)


@router.delete("/users/{user_id}/events/workouts/{workout_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workout(
    user_id: UUID,
    workout_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> None:
    """Delete a workout session."""
    if not event_record_service.delete_event_record(db, user_id, workout_id, "workout"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workout not found")


@router.delete("/users/{user_id}/events/sleep/{sleep_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sleep_session(
    user_id: UUID,
    sleep_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> None:
    """Delete a sleep session."""
    if not event_record_service.delete_event_record(db, user_id, sleep_id, "sleep"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sleep session not found")


@router.delete("/users/{user_id}/events/menstrual-cycles/{cycle_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_menstrual_cycle(
    user_id: UUID,
    cycle_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> None:
    """Delete a menstrual cycle record."""
    if not event_record_service.delete_event_record(db, user_id, cycle_id, "menstrual_cycle"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Menstrual cycle record not found")


@router.delete("/users/{user_id}/events/meals/{meal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meal(
    user_id: UUID,
    meal_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> None:
    """Delete a meal together with its nutrients."""
    if not event_record_service.delete_event_record(db, user_id, meal_id, "meal"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meal not found")
