import contextlib
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import cast

from celery import shared_task

from app.config import settings
from app.database import SessionLocal
from app.integrations.redis_client import get_redis_client
from app.services.apple.healthkit.sleep_service import (
    active_member,
    active_users_key,
    finish_sleep,
    load_sleep_state,
    parse_active_member,
)
from app.utils.sentry_helpers import log_and_capture_error

logger = getLogger(__name__)


@shared_task
def finalize_stale_sleeps() -> None:
    now = datetime.now(timezone.utc)
    redis_client = get_redis_client()

    with SessionLocal() as db:
        # Members identify a (user, reporting stream) pair; a member written by an
        # older release is the bare user id and parses back to an empty scope, which
        # still addresses the key that release wrote.
        for member in cast(set[str], redis_client.smembers(active_users_key())):
            user_id, scope = parse_active_member(member)
            try:
                # Skip users whose upload is currently in progress. The lock stays
                # per-user so a batch spanning several streams is still serialised
                # against this sweep as a whole.
                lock = redis_client.lock(f"sleep:lock:{user_id}", timeout=30, blocking_timeout=0)
                if not lock.acquire(blocking=False):
                    continue

                try:
                    state = load_sleep_state(user_id, scope)
                    if not state:
                        # The state expired or was finalised elsewhere; drop the
                        # member so the set cannot grow without bound.
                        redis_client.srem(active_users_key(), active_member(user_id, scope))
                        continue

                    end_time = state.end_time
                    if end_time.tzinfo is None:
                        end_time = end_time.replace(tzinfo=timezone.utc)

                    if now - end_time >= timedelta(minutes=settings.sleep_end_gap_minutes):
                        finish_sleep(db, user_id, state)
                finally:
                    with contextlib.suppress(Exception):
                        lock.release()

            except Exception as e:
                log_and_capture_error(
                    e,
                    logger,
                    f"Error finalizing stale sleep for user {user_id}: {e}",
                    extra={"user_id": user_id},
                )
