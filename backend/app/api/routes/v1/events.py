from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.database import DbSession
from app.schemas.model_crud.activities import EventRecordQueryParams
from app.schemas.responses.activity import (
    MenstrualCycleRecord,
    SleepSession,
    Workout,
)
from app.schemas.utils import PaginatedResponse
from app.services import ApiKeyDep
from app.services.event_record_service import event_record_service
from app.utils.dates import DateTimeQueryParam, parse_query_datetime

router = APIRouter()


@router.get("/users/{user_id}/events/workouts")
def list_workouts(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    record_type: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PaginatedResponse[Workout]:
    """Returns workout sessions."""
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_datetime(end_date),
        cursor=cursor,
        limit=limit,
        record_type=record_type,
    )
    return event_record_service.get_workouts(db, user_id, params)


@router.get("/users/{user_id}/events/sleep")
def list_sleep_sessions(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    filter_by_priority: Annotated[
        bool,
        Query(
            description="When true, keep only the highest-priority source's sessions per sleep date "
            "(provider/device priority, same ranking as summaries). Defaults to false for backwards compatibility."
        ),
    ] = False,
) -> PaginatedResponse[SleepSession]:
    """Returns sleep sessions (including naps)."""
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_datetime(end_date),
        cursor=cursor,
        limit=limit,
    )
    return event_record_service.get_sleep_sessions(db, user_id, params, filter_by_priority=filter_by_priority)


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


@router.get("/users/{user_id}/events/menstrual-cycles")
def list_menstrual_cycles(
    user_id: UUID,
    start_date: DateTimeQueryParam,
    end_date: DateTimeQueryParam,
    db: DbSession,
    _api_key: ApiKeyDep,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PaginatedResponse[MenstrualCycleRecord]:
    """Returns menstrual cycle records."""
    params = EventRecordQueryParams(
        start_datetime=parse_query_datetime(start_date),
        end_datetime=parse_query_datetime(end_date),
        cursor=cursor,
        limit=limit,
    )
    return event_record_service.get_menstrual_cycles(db, user_id, params)


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
