"""Garmin wellness webhook handler.

Processes a batch of wellness notifications for a single data type.
Supports both PUSH (inline records) and PING (callbackURL) per notification.
"""

import logging
from typing import Any
from uuid import UUID

from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.schemas.sync_status import SyncSource, SyncStatus
from app.services.providers.garmin.backfill_state import get_trace_id
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.sync_status_service import emit_sync_completed, new_run_id
from app.utils.connection_context import active_connection
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


def process_wellness_items(
    db: DbSession,
    connection_repo: UserConnectionRepository,
    garmin_247: Garmin247Data,
    summary_type: str,
    notifications: list[dict[str, Any]],
    errors: list[str],
    synced_user_ids: set[UUID],
    request_trace_id: str,
) -> dict[str, Any]:
    """Process wellness notifications for a single data type.

    Detects PING (items have ``callbackURL``) vs PUSH (inline data) per item.
    Groups resolved records by *connection* before calling ``process_items_batch``
    to minimise DB round-trips.  All OW profiles sharing a Garmin account
    receive the data; the first connection per garmin_user_id is primary
    (WEBHOOK source), any others are secondaries (LINKED_ACCOUNT source).

    The grouping key is the connection rather than the user because one user may
    hold several Garmin accounts: keying by user would pool two watches' batches
    into one call, and whichever account resolved first would take credit for
    both units' data.

    Returns:
        {"processed": int, "saved": int, "succeeded_users": list[str]}
    """
    # (user_id, connection_id) -> notifications for that account.
    connection_items: dict[tuple[UUID, UUID], list[dict[str, Any]]] = {}
    # Maps a secondary (user_id, connection_id) → primary user_id for LINKED_ACCOUNT events.
    secondary_primary: dict[tuple[UUID, UUID], UUID] = {}

    for notification in notifications:
        garmin_user_id: str | None = notification.get("userId")
        if not garmin_user_id:
            log_structured(
                logger,
                "warning",
                "No user ID in notification",
                provider="garmin",
                trace_id=request_trace_id,
                summary_type=summary_type,
            )
            errors.append(f"{summary_type}: missing userId")
            continue

        connections = connection_repo.get_all_by_provider_user_id(db, "garmin", garmin_user_id)
        if not connections:
            log_structured(
                logger,
                "warning",
                "No connection found for Garmin user",
                provider="garmin",
                trace_id=request_trace_id,
                summary_type=summary_type,
                garmin_user_id=garmin_user_id,
            )
            errors.append(f"User {garmin_user_id} not connected")
            continue

        if "callbackURL" in notification and summary_type != "activityFiles":
            # PING not supported; Garmin must be configured for PUSH-only delivery.
            # Exception: activityFiles always carries a callbackURL for FIT download.
            continue

        primary_user_id = connections[0].user_id
        for i, connection in enumerate(connections):
            key = (connection.user_id, connection.id)
            connection_items.setdefault(key, []).append(notification)
            if i > 0:
                secondary_primary.setdefault(key, primary_user_id)

    total_processed = sum(len(items) for items in connection_items.values())
    total_saved = 0
    succeeded_users: list[str] = []

    for (uid, connection_id), items in connection_items.items():
        trace_id = get_trace_id(uid) or request_trace_id
        is_secondary = (uid, connection_id) in secondary_primary
        sync_source = SyncSource.LINKED_ACCOUNT if is_secondary else SyncSource.WEBHOOK
        try:
            # Bind the account: process_items_batch re-resolves the connection
            # from (user, provider) to stamp data sources and download FIT files.
            with active_connection(connection_id):
                count = garmin_247.process_items_batch(db, uid, summary_type, items)
            total_saved += count
            synced_user_ids.add(uid)
            succeeded_users.append(str(uid))
            log_structured(
                logger,
                "debug" if is_secondary else "info",
                "Saved wellness data",
                provider="garmin",
                trace_id=trace_id,
                summary_type=summary_type,
                saved=count,
                user_id=str(uid),
                is_secondary=is_secondary,
            )
            emit_sync_completed(
                uid,
                "garmin",
                sync_source,
                run_id=new_run_id(prefix=f"garmin_webhook_{summary_type}"),
                status=SyncStatus.SUCCESS,
                message=f"Garmin live data received: {summary_type}",
                items_processed=count,
                primary_user_id=secondary_primary.get((uid, connection_id)),
                metadata={
                    "trace_id": trace_id,
                    "summary_type": summary_type,
                    "items": len(items),
                    "connection_id": str(connection_id),
                },
            )
        except Exception as e:
            log_structured(
                logger,
                "error",
                f"Error processing {summary_type}",
                provider="garmin",
                trace_id=trace_id,
                user_id=str(uid),
                error=str(e),
            )
            errors.append(f"{summary_type} error: {e}")

    return {"processed": total_processed, "saved": total_saved, "succeeded_users": succeeded_users}
