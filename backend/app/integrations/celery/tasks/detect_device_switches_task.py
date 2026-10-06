from logging import getLogger
from uuid import UUID

from celery import shared_task
from sqlalchemy import select

from app.database import SessionLocal
from app.models import UserConnection
from app.services.device_timeline_service import device_timeline_service
from app.utils.device_switch_detection import EVIDENCE_PROVIDERS
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


@shared_task
def detect_device_switches() -> dict:
    """Keep each account's dated device history in line with its provider's workouts.

    For every account whose provider names the device on workouts (Garmin, Polar,
    Suunto, Fitbit) and whose history is automatic: a watch that starts appearing on
    workouts starts a period at its first one, and the records stored since are
    re-filed to it. An unchanged account costs one query and writes nothing.

    A sweep rather than a hook on ingest: workouts arrive by webhook, by pull and by
    backfill, in any order and alongside the nights they bracket. Re-reading the
    account's workouts on a schedule sees all of them the same way, and the re-file
    that follows a change catches records filed in the meantime.

    Each account is its own transaction, so one that fails is logged and skipped.
    """
    with SessionLocal() as db:
        connection_ids: list[UUID] = list(
            db.execute(
                select(UserConnection.id).where(
                    UserConnection.provider.in_([p.value for p in EVIDENCE_PROVIDERS]),
                    UserConnection.device_timeline_auto.is_(True),
                )
            )
            .scalars()
            .all()
        )

    changed: list[str] = []
    failed: list[str] = []
    for connection_id in connection_ids:
        with SessionLocal() as db:
            connection = db.get(UserConnection, connection_id)
            if connection is None:
                continue
            try:
                result = device_timeline_service.detect(db, connection)
            except Exception:
                db.rollback()
                failed.append(str(connection_id))
                logger.exception("device switch detection failed for connection %s", connection_id)
                continue
            if result.changed:
                changed.append(str(connection_id))
                moved = result.refile.total_moved if result.refile else None
                log_structured(
                    logger,
                    "info",
                    "Device history updated from workouts",
                    action="device_switch_detected",
                    user_connection_id=str(connection_id),
                    periods=len(result.timeline.periods),
                    moved=moved.model_dump() if moved else None,
                )

    return {"checked": len(connection_ids), "changed": changed, "failed": failed}
