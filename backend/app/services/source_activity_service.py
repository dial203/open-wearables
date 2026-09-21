"""Assembles the per-source preview the device registry reads.

Answers one question - "what is this thing, and whose account is it on" - for every
data source a user has, in a fixed number of queries. The registry's own naming is
often no help: a source called "Bluetooth Device" or a bare bundle id identifies
nothing, and the only evidence left is what it reported and how recently.
"""

from logging import Logger
from uuid import UUID

from sqlalchemy import select

from app.database import DbSession
from app.models import DataSource, UserConnection
from app.repositories.source_activity_repository import SourceActivityRepository, SourceActivityRow
from app.schemas.model_crud.devices import (
    ActivityBucket,
    SourceActivityListResponse,
    SourceActivityResponse,
)
from app.schemas.model_crud.devices.activity import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    MIN_WINDOW_DAYS,
)
from app.schemas.model_crud.user_management import account_display_label
from app.utils.exceptions import handle_exceptions

# How many time-series codes to return per source. A source can carry dozens; the
# question here is what kind of hardware this is, and the busiest handful answers it.
# Cut on the server so the payload does not grow with a participant's history.
MAX_METRICS_PER_SOURCE = 8


class SourceActivityService:
    def __init__(self, log: Logger):
        self.logger = log
        self.repo = SourceActivityRepository()

    @handle_exceptions
    def get_user_source_activity(
        self,
        db: DbSession,
        user_id: UUID,
        days: int = DEFAULT_WINDOW_DAYS,
    ) -> SourceActivityListResponse:
        window = max(MIN_WINDOW_DAYS, min(MAX_WINDOW_DAYS, days))

        sources = list(db.scalars(select(DataSource).where(DataSource.user_id == user_id)).all())
        accounts = {
            connection.id: connection
            for connection in db.scalars(select(UserConnection).where(UserConnection.user_id == user_id)).all()
        }

        events = _by_source(self.repo.events(db, user_id, window))
        scores = _by_source(self.repo.scores(db, user_id, window))
        metrics = _by_source(self.repo.metrics(db, user_id, window))

        items = [
            self._to_response(
                source,
                accounts.get(source.user_connection_id),
                events.get(source.id, []),
                scores.get(source.id, []),
                metrics.get(source.id, []),
            )
            for source in sources
        ]
        # Most recently active first, and sources that reported nothing in the window
        # last. A quiet source is still worth showing - it is a device that came off, or
        # a pairing that broke - but it is not what someone scanning this is looking for.
        items.sort(key=lambda item: (item.last_seen_at is None, _descending(item)), reverse=False)
        return SourceActivityListResponse(items=items, total=len(items), window_days=window)

    def _to_response(
        self,
        source: DataSource,
        connection: UserConnection | None,
        events: list[SourceActivityRow],
        scores: list[SourceActivityRow],
        metrics: list[SourceActivityRow],
    ) -> SourceActivityResponse:
        buckets = [*events, *scores, *metrics]
        return SourceActivityResponse(
            data_source_id=source.id,
            provider=getattr(source.provider, "value", source.provider),
            source=source.source,
            device_model=source.device_model,
            device_id=source.device_id,
            user_connection_id=source.user_connection_id,
            account_label=(
                None
                if connection is None
                else account_display_label(
                    connection.account_label,
                    connection.provider_username,
                    connection.account_email,
                    connection.provider,
                    connection.id,
                )
            ),
            account_email=None if connection is None else connection.account_email,
            account_type=None if connection is None else connection.account_type,
            events=_to_buckets(events),
            scores=_to_buckets(scores),
            # Busiest first, then capped: a ring's heart-rate stream says more about the
            # hardware than the twentieth metric it happens to also carry.
            metrics=_to_buckets(sorted(metrics, key=lambda r: r.count, reverse=True)[:MAX_METRICS_PER_SOURCE]),
            last_seen_at=max((row.last_at for row in buckets), default=None),
        )


def _by_source(rows: list[SourceActivityRow]) -> dict[UUID, list[SourceActivityRow]]:
    grouped: dict[UUID, list[SourceActivityRow]] = {}
    for row in rows:
        grouped.setdefault(row.data_source_id, []).append(row)
    return grouped


def _to_buckets(rows: list[SourceActivityRow]) -> list[ActivityBucket]:
    return [
        ActivityBucket(label=row.label, count=row.count, first_at=row.first_at, last_at=row.last_at) for row in rows
    ]


def _descending(item: SourceActivityResponse) -> float:
    """Sort key that puts the most recent first without reversing the quiet-last rule."""
    return 0.0 if item.last_seen_at is None else -item.last_seen_at.timestamp()
