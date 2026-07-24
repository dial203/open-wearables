import json
from datetime import datetime
from json import JSONDecodeError
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, status
from pydantic import ValidationError

from app.database import DbSession
from app.integrations.celery.tasks.process_xml_upload_task import process_xml_upload
from app.schemas.providers.apple.apple_xml import (
    PresignedURLRequest,
    PresignedURLResponse,
    SNSNotification,
)
from app.schemas.responses.upload import UploadDataResponse
from app.services import ApiKeyDep
from app.services.apple.apple_xml.presigned_url_service import presigned_url_service
from app.services.apple.apple_xml.sns_service import sns_service
from app.services.polar_rr_import_service import PolarRrImportError, polar_rr_import_service

router = APIRouter()


@router.post("/users/{user_id}/import/apple/xml/s3")
def import_xml_presigned_url(
    user_id: str,
    request: PresignedURLRequest,
    _api_key: ApiKeyDep,
) -> PresignedURLResponse:
    """Generate presigned URL for XML file upload and trigger processing task."""
    return presigned_url_service.create_presigned_url(user_id, request)


@router.post("/users/{user_id}/import/apple/xml/direct")
def import_xml_file(
    user_id: str,
    file: UploadFile,
    _api_key: ApiKeyDep,
) -> dict[str, str]:
    """Import XML file into the database."""
    file_contents = file.file.read()
    filename = file.filename or "upload.xml"

    task = process_xml_upload.delay(file_contents=file_contents, filename=filename, user_id=user_id)

    return {
        "status": "processing",
        "task_id": task.id,
        "user_id": user_id,
    }


@router.post("/users/{user_id}/import/polar/rr")
def import_polar_rr(
    user_id: UUID,
    file: UploadFile,
    db: DbSession,
    _api_key: ApiKeyDep,
    workout_id: Annotated[
        UUID | None, Query(description="OW workout id this RR recording belongs to")
    ] = None,
    start_time: Annotated[
        datetime | None,
        Query(
            description="Session start time (ISO 8601, ideally with offset) to attribute the RR by, "
            "when the OW workout id isn't known — e.g. Polar Flow exposes only its own session id."
        ),
    ] = None,
    device: Annotated[
        str | None, Query(description="Device model for the RR source (e.g. 'Polar Grit X2 Pro'); used with start_time")
    ] = None,
) -> dict[str, int | str]:
    """Import a Polar Flow RR-interval CSV.

    The CSV is Polar Flow's per-exercise RR export (`duration,offline` header, one
    beat-to-beat interval in ms per row). Per-beat timestamps are reconstructed from the
    session start; the data is stored as an `rr_interval` time series on the Polar source
    and is then queryable via `/timeseries?types=rr_interval`.

    Provide exactly one of `workout_id` (ties to a known OW workout) or `start_time`
    (attributes by session start when the OW workout id isn't known). 404 if the workout
    doesn't exist for this user; 400 if the CSV is malformed or the params are invalid.
    """
    if (workout_id is None) == (start_time is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide exactly one of workout_id or start_time",
        )

    contents = file.file.read()
    try:
        if workout_id is not None:
            result = polar_rr_import_service.import_rr_csv(db, user_id, workout_id, contents)
            if result is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workout not found for this user")
            return result
        assert start_time is not None
        return polar_rr_import_service.import_rr_csv_by_start_time(db, user_id, start_time, contents, device=device)
    except PolarRrImportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/sns/notification", status_code=status.HTTP_202_ACCEPTED)
async def receive_sns_notification(
    request: Request,
) -> UploadDataResponse:
    """Handle all SNS messages (subscription confirmation + S3 upload notifications)."""
    body = await request.body()
    try:
        notification = SNSNotification.model_validate(json.loads(body))
    except (ValidationError, JSONDecodeError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    result = await sns_service.handle_sns_notification(notification)

    if result.status_code not in (status.HTTP_200_OK, status.HTTP_202_ACCEPTED):
        raise HTTPException(status_code=result.status_code, detail=result.response)
    return result
