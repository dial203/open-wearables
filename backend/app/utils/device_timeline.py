"""An account's dated device history, and which device it names at a given instant.

``user_connection.device_label`` answers "what device is behind this account" and
never "when". A person who changes watch on an account whose provider reports no
device - Garmin's wellness summaries, Whoop - then has every record after the switch
filed under the old model, and nothing on the row can reveal it: the old model's
nights and the new model's look the same once written.

A timeline is that account's periods, each naming a label from its ``effective_from``
until the next one's. Three rules, each chosen over a more convenient alternative:

- **By the record's own time, not the moment it arrived.** A night recorded before
  the switch and synced after it belongs to the old device. Resolving at ingest time
  would file every late sync and every backfill under whatever is worn today.
- **An unstated stretch resolves to nothing.** A timestamp before the earliest period,
  when that period has a start, is covered by no statement, and gets no label rather
  than the nearest one. The data lands on a model-less source, which a person can see
  and attribute; a guessed label would be indistinguishable from a stated one.
- **Only a gap the provider left.** A model the provider reported on the record is a
  capture and always wins; the timeline fills ``device_model`` only where the payload
  named none, exactly as the undated label always did.

A caller with no timestamp (``at`` None) gets the current period - the latest - which
is what the undated label meant, so such callers behave as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.database import DbSession
from app.models import UserConnectionDevicePeriod


@dataclass(frozen=True)
class DevicePeriod:
    device_label: str
    # None: from the start of the account's data.
    effective_from: datetime | None


@dataclass(frozen=True)
class DeviceSpan:
    """One period as a half-open interval ``[start, end)``; None is unbounded."""

    device_label: str
    start: datetime | None
    end: datetime | None


def _utc(at: datetime) -> datetime:
    # Stored instants are timestamptz; a naive value reaching here is UTC, which is
    # how Postgres itself would read it in this app's sessions. Comparing naive with
    # aware raises, and a crash mid-ingest would drop the whole batch.
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class DeviceTimeline:
    """An account's periods in order, the open-start one (if any) first."""

    periods: tuple[DevicePeriod, ...]

    def __post_init__(self) -> None:
        starts = [p.effective_from for p in self.periods]
        if starts.count(None) > 1:
            raise ValueError("a timeline has at most one period without a start")
        if None in starts and starts[0] is not None:
            raise ValueError("the period without a start must come first")
        dated = [_utc(s) for s in starts if s is not None]
        if dated != sorted(dated) or len(set(dated)) != len(dated):
            raise ValueError("periods must be in strictly increasing order of effective_from")

    @property
    def current_label(self) -> str | None:
        return self.periods[-1].device_label if self.periods else None

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(p.device_label for p in self.periods)

    def label_at(self, at: datetime | None) -> str | None:
        """The label stated for ``at``; None when no period covers it."""
        if not self.periods:
            return None
        if at is None:
            return self.current_label
        at = _utc(at)
        chosen: DevicePeriod | None = None
        for period in self.periods:
            if period.effective_from is None or _utc(period.effective_from) <= at:
                chosen = period
            else:
                break
        return chosen.device_label if chosen else None

    def spans(self) -> list[DeviceSpan]:
        spans: list[DeviceSpan] = []
        for i, period in enumerate(self.periods):
            end = self.periods[i + 1].effective_from if i + 1 < len(self.periods) else None
            spans.append(DeviceSpan(period.device_label, period.effective_from, end))
        return spans


def load_timeline(db_session: DbSession, user_connection_id: UUID | None) -> DeviceTimeline | None:
    """The account's timeline, or None when it has no periods (undated label applies)."""
    if user_connection_id is None:
        return None
    rows = db_session.execute(
        select(UserConnectionDevicePeriod.device_label, UserConnectionDevicePeriod.effective_from)
        .where(UserConnectionDevicePeriod.user_connection_id == user_connection_id)
        .order_by(UserConnectionDevicePeriod.effective_from.asc().nulls_first())
    ).all()
    if not rows:
        return None
    return DeviceTimeline(tuple(DevicePeriod(label, start) for label, start in rows))
