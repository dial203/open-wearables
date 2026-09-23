"""retime Polar optical PPI beats to true UTC

Revision ID: f3a9c1e7b2d4
Revises: c6d2a8f4e1b3

AccessLink v4 gives each PPI beat as ``offsetMillis`` from the start of its ``date``, and
neither carries a zone: the day is the watch's local calendar day. The v4 path added the
offset to a naive local midnight and stored the result, which Postgres reads as UTC, so
every ``pulse_to_pulse_interval`` beat sat one UTC offset away from when it happened - 4 h
early in US Eastern daylight time, and off the H10 ``rr_interval`` it is compared against.
The code is fixed alongside this; this repairs what is already stored.

What moves: ``pulse_to_pulse_interval`` rows on Polar sources whose ``zone_offset`` is NULL.
The old path never set ``zone_offset`` on PPI and the fixed one always does, so NULL is
exactly the stale set, and a pull on the fixed code that lands before this runs is left
alone rather than shifted twice.

By how much: each stale row's UTC date is its local PPI day (it was stored as local midnight
plus an offset under 24 h). The day's offset comes from the user's Polar record, on any of
their Polar sources, with a ``zone_offset`` whose start is nearest that day's local midnight
and within 3 days of it - the same rule, and the same window, the fixed code uses. Polar
exercises carry the watch's own ``start_time_utc_offset``, so this is what the device was
set to, DST and travel included. It runs after c6d2a8f4e1b3, which put those exercises at
true UTC.

What does not move: a day with no such record nearby. The live path would fall back to the
account's timezone setting, which a migration cannot fetch, and guessing an offset would
leave a wrong row that can no longer be told from a right one. Those rows keep their NULL
``zone_offset`` and are counted in the log; they can be found afterwards with
``series_type_definition_id = <pulse_to_pulse_interval> AND zone_offset IS NULL`` on a Polar
source.

Where a beat's corrected time is already taken on its source (a pull on the fixed code got
there first), the stale beat is deleted rather than moved onto it: same source, type and
instant is the same beat. Rows are parked far in the future while they move so a shift
cannot collide with a stale row that has not moved yet.

Run once by Alembic: a shift is not idempotent, so it must not live in the re-runnable
startup scripts. Downgrade refuses rather than set up a second shift.
"""

import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a9c1e7b2d4"
down_revision: Union[str, None] = "c6d2a8f4e1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_ZONE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
# Mirrors PPI_OFFSET_WINDOW in app/services/providers/polar/v4_data.py.
_WINDOW = timedelta(days=3)
# Far enough out that no real sample can sit there; parked rows are recognised by it.
_PARK = "interval '1000 years'"
_PARKED = "recorded_at > now() + interval '500 years'"


def _offset_minutes(zone_offset: str | None) -> int | None:
    match = _ZONE.match(zone_offset or "")
    if not match:
        return None
    sign, hours, minutes = match.groups()
    total = int(hours) * 60 + int(minutes)
    return -total if sign == "-" else total


def _zone(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def _nearest_offset(day: date, anchors: list[tuple[datetime, int]]) -> int | None:
    best: tuple[timedelta, int] | None = None
    for start, minutes in anchors:
        midnight = datetime.combine(day, time(), tzinfo=timezone(timedelta(minutes=minutes)))
        distance = abs(start - midnight)
        if distance <= _WINDOW and (best is None or distance < best[0]):
            best = (distance, minutes)
    return best[1] if best else None


def upgrade() -> None:
    conn = op.get_bind()
    ppi_type_id = conn.execute(
        sa.text("SELECT id FROM series_type_definition WHERE code = 'pulse_to_pulse_interval'")
    ).scalar()
    if ppi_type_id is None:
        return

    days = conn.execute(
        sa.text(
            "SELECT p.data_source_id, ds.user_id, (p.recorded_at AT TIME ZONE 'UTC')::date AS day, count(*) AS beats "
            "FROM data_point_series p JOIN data_source ds ON ds.id = p.data_source_id "
            "WHERE ds.provider = 'polar' AND p.series_type_definition_id = :ppi AND p.zone_offset IS NULL "
            "GROUP BY 1, 2, 3"
        ),
        {"ppi": ppi_type_id},
    ).all()
    if not days:
        return

    anchors: dict = defaultdict(list)
    for row in conn.execute(
        sa.text(
            "SELECT ds.user_id, er.start_datetime, er.zone_offset "
            "FROM event_record er JOIN data_source ds ON ds.id = er.data_source_id "
            "WHERE ds.provider = 'polar' AND er.zone_offset IS NOT NULL AND ds.user_id = ANY(:users)"
        ),
        {"users": list({d.user_id for d in days})},
    ):
        minutes = _offset_minutes(row.zone_offset)
        if minutes is not None:
            start = row.start_datetime
            anchors[row.user_id].append((start if start.tzinfo else start.replace(tzinfo=timezone.utc), minutes))

    base = "data_source_id = :ds AND series_type_definition_id = :ppi"
    moved = unresolved = unresolved_days = 0
    sources: set = set()
    for d in days:
        minutes = _nearest_offset(d.day, anchors[d.user_id])
        if minutes is None:
            unresolved += d.beats
            unresolved_days += 1
            continue
        day_start = datetime.combine(d.day, time(), tzinfo=timezone.utc)
        moved += conn.execute(
            sa.text(
                f"UPDATE data_point_series SET recorded_at = recorded_at - :shift + {_PARK}, zone_offset = :zone "
                f"WHERE {base} AND zone_offset IS NULL AND recorded_at >= :lo AND recorded_at < :hi"
            ),
            {
                "ds": d.data_source_id,
                "ppi": ppi_type_id,
                "shift": timedelta(minutes=minutes),
                "zone": _zone(minutes),
                "lo": day_start,
                "hi": day_start + timedelta(days=1),
            },
        ).rowcount
        sources.add(d.data_source_id)

    dropped = 0
    for source in sources:
        params = {"ds": source, "ppi": ppi_type_id}
        dropped += conn.execute(
            sa.text(
                "DELETE FROM data_point_series p WHERE p.data_source_id = :ds AND p.series_type_definition_id = :ppi "
                f"AND p.{_PARKED} AND EXISTS (SELECT 1 FROM data_point_series q "
                "WHERE q.data_source_id = p.data_source_id "
                "AND q.series_type_definition_id = p.series_type_definition_id "
                f"AND q.recorded_at = p.recorded_at - {_PARK})"
            ),
            params,
        ).rowcount
        conn.execute(
            sa.text(f"UPDATE data_point_series SET recorded_at = recorded_at - {_PARK} WHERE {base} AND {_PARKED}"),
            params,
        )

    logger.info(
        "Polar PPI retime: %d beats moved, %d dropped as stale duplicates; "
        "%d beats on %d source-days left in place with no Polar offset within %d days",
        moved - dropped,
        dropped,
        unresolved,
        unresolved_days,
        _WINDOW.days,
    )


def downgrade() -> None:
    # A no-op downgrade would let the next upgrade shift everything a second time.
    raise RuntimeError(
        "f3a9c1e7b2d4 retimed stored Polar PPI and cannot be reversed. To move the schema "
        "below it without re-running the shift, use `alembic stamp c6d2a8f4e1b3`."
    )
