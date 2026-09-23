"""retime Polar exercises and their RR beats to true UTC

Revision ID: c6d2a8f4e1b3
Revises: b4e8f2a7c915

Polar's exercise ``start_time`` is naive local time and ``start_time_utc_offset`` is that
zone's offset in minutes, so UTC is local minus the offset. The workouts path added it,
which stored every Polar exercise - and every RR beat reconstructed from it - twice the
offset away from when it happened. In US Eastern daylight time (offset -240) that is 8 h
early: an overnight H10 session sat on the previous afternoon, and a read over the sleep
window found none of its beats. The code is fixed alongside this; this repairs what is
already stored.

What moves, by ``-2 * offset`` taken from each workout's own stored ``zone_offset``:

* Polar workout event records.
* RR beats reconstructed from AccessLink v3 sample type 11. They carry the exercise id as
  ``external_id``, so they are tied to their workout unambiguously.
* RR beats imported from a Polar Flow CSV *by workout id*. Those were anchored to the
  workout's stored (wrong) start and carry no ``external_id``, so they are recognised by
  lying inside the stored session window. A CSV imported *by start time* was anchored to
  the caller's own timezone-aware start and is already right; where one sits inside the
  corrected window, the part of the stored window it overlaps is ambiguous and is left
  where it is rather than risk moving good beats onto a wrong clock.

What does not move: v4 RR beats (the v4 match compared against the mis-shifted start, so
outside UTC+0 it never matched and none were written), Polar sleep and 24/7 data (a
different path that parses offset-aware times), and anything from other providers.

Where a beat's corrected time is already taken on its source, the stale beat is deleted
rather than moved onto it (same source, type and instant is the same beat). Where a
workout's corrected slot holds the same exercise, the stale workout is deleted; where it
holds a different record (the unique key is source and times, whatever the category), the
workout is left where it was and logged. Rows are parked far in the future while they move
so a shift cannot collide with a row of the same set that has not moved yet.

Deploy with the code fix and let this run before the workers pull Polar again. A pull on
the old code after this has run writes wrongly timed rows again; a pull on the fixed code
before it writes correct rows that this cannot tell from stale ones.

Run once by Alembic, which is the point: a shift is not idempotent, so this must not live
in the re-runnable startup scripts.

Not reversible: undoing the shift would reinstate a known-wrong clock, and rows deleted as
stale duplicates cannot be recovered. Downgrade refuses rather than set up a second shift.
"""

import logging
import re
from collections.abc import Sequence
from datetime import timedelta
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "c6d2a8f4e1b3"
down_revision: Union[str, None] = "b4e8f2a7c915"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_ZONE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
# Beats are timestamped at the R-wave closing each interval, so a series can run a little
# past the session's recorded end (v4 and CSV clocks are not stretched to fit it).
_SLACK = timedelta(minutes=10)
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


def _retime_beats(conn: sa.Connection, rr_type_id: int, workout: sa.Row, delta: timedelta) -> tuple[int, int, int]:
    """Move one workout's RR beats by ``delta``. Returns (moved, dropped_as_stale, left_ambiguous)."""
    params = {
        "ds": workout.data_source_id,
        "rr": rr_type_id,
        "ext": workout.external_id,
        "s_start": workout.start_datetime,
        "s_end": workout.end_datetime + _SLACK,
        "c_start": workout.start_datetime + delta,
        "c_end": workout.end_datetime + delta + _SLACK,
    }
    base = "data_source_id = :ds AND series_type_definition_id = :rr"
    in_stored = "recorded_at BETWEEN :s_start AND :s_end"
    in_corrected = "recorded_at BETWEEN :c_start AND :c_end"

    # A correctly timed CSV copy shows up as untagged beats in the corrected window that the
    # stored window does not reach. If there is one, the overlap of the two windows holds
    # beats of both kinds and cannot be told apart.
    has_true_copy = conn.execute(
        sa.text(
            f"SELECT EXISTS (SELECT 1 FROM data_point_series WHERE {base} AND external_id IS NULL "
            f"AND {in_corrected} AND NOT ({in_stored}))"
        ),
        params,
    ).scalar()
    untagged = f"external_id IS NULL AND {in_stored}"
    ambiguous = 0
    if has_true_copy:
        ambiguous = conn.execute(
            sa.text(f"SELECT count(*) FROM data_point_series WHERE {base} AND {untagged} AND {in_corrected}"),
            params,
        ).scalar()
        untagged += f" AND NOT ({in_corrected})"

    tagged = "external_id = :ext" if workout.external_id is not None else "FALSE"
    moved = conn.execute(
        sa.text(
            f"UPDATE data_point_series SET recorded_at = recorded_at + :delta + {_PARK} "
            f"WHERE {base} AND (({tagged}) OR ({untagged}))"
        ),
        {**params, "delta": delta},
    ).rowcount
    dropped = conn.execute(
        sa.text(
            f"DELETE FROM data_point_series p WHERE p.data_source_id = :ds AND p.series_type_definition_id = :rr "
            f"AND p.{_PARKED} AND EXISTS (SELECT 1 FROM data_point_series q "
            "WHERE q.data_source_id = p.data_source_id AND q.series_type_definition_id = p.series_type_definition_id "
            f"AND q.recorded_at = p.recorded_at - {_PARK})"
        ),
        params,
    ).rowcount
    conn.execute(
        sa.text(f"UPDATE data_point_series SET recorded_at = recorded_at - {_PARK} WHERE {base} AND {_PARKED}"),
        params,
    )
    return moved - dropped, dropped, ambiguous


def upgrade() -> None:
    conn = op.get_bind()
    rr_type_id = conn.execute(sa.text("SELECT id FROM series_type_definition WHERE code = 'rr_interval'")).scalar()

    workouts = conn.execute(
        sa.text(
            "SELECT er.id, er.data_source_id, er.external_id, er.start_datetime, er.end_datetime, er.zone_offset "
            "FROM event_record er JOIN data_source ds ON ds.id = er.data_source_id "
            "WHERE ds.provider = 'polar' AND er.category = 'workout'"
        )
    ).all()

    shifts: dict = {}
    beats_moved = beats_dropped = beats_ambiguous = 0
    for workout in workouts:
        offset = _offset_minutes(workout.zone_offset)
        if not offset:
            continue  # UTC+0 was never wrong; a missing offset cannot be corrected
        delta = timedelta(minutes=-2 * offset)
        shifts[workout.id] = delta
        if rr_type_id is not None:
            moved, dropped, ambiguous = _retime_beats(conn, rr_type_id, workout, delta)
            beats_moved += moved
            beats_dropped += dropped
            beats_ambiguous += ambiguous

    # Workouts after their beats, which are located by the workout's stored window. All of
    # them are parked first so no workout is judged a duplicate of one that has yet to move.
    for workout_id, delta in shifts.items():
        conn.execute(
            sa.text(
                f"UPDATE event_record SET start_datetime = start_datetime + :delta + {_PARK}, "
                f"end_datetime = end_datetime + :delta + {_PARK} WHERE id = :id"
            ),
            {"id": workout_id, "delta": delta},
        )
    ids = list(shifts)
    workouts_dropped = workouts_blocked = 0
    if ids:
        occupant = (
            "SELECT 1 FROM event_record q WHERE q.data_source_id = p.data_source_id "
            f"AND q.start_datetime = p.start_datetime - {_PARK} AND q.end_datetime = p.end_datetime - {_PARK}"
        )
        # The same exercise already at its corrected time is a stale copy of it.
        workouts_dropped = conn.execute(
            sa.text(
                f"DELETE FROM event_record p WHERE p.id = ANY(:ids) AND EXISTS ({occupant} "
                "AND q.category = p.category AND q.external_id IS NOT DISTINCT FROM p.external_id)"
            ),
            {"ids": ids},
        ).rowcount
        # Anything else in that slot (the unique key is source and times, whatever the
        # category) is a different record. It is not ours to delete, so the workout stays
        # where it was and is reported.
        blocked = (
            conn.execute(
                sa.text(f"SELECT p.id FROM event_record p WHERE p.id = ANY(:ids) AND EXISTS ({occupant})"),
                {"ids": ids},
            )
            .scalars()
            .all()
        )
        for workout_id in blocked:
            conn.execute(
                sa.text(
                    f"UPDATE event_record SET start_datetime = start_datetime - {_PARK} - :delta, "
                    f"end_datetime = end_datetime - {_PARK} - :delta WHERE id = :id"
                ),
                {"id": workout_id, "delta": shifts[workout_id]},
            )
        workouts_blocked = len(blocked)
        if blocked:
            logger.warning("Polar retime: workouts left unmoved, slot held by another record: %s", blocked)
        conn.execute(
            sa.text(
                f"UPDATE event_record SET start_datetime = start_datetime - {_PARK}, "
                f"end_datetime = end_datetime - {_PARK} WHERE id = ANY(:ids) "
                "AND start_datetime > now() + interval '500 years'"
            ),
            {"ids": ids},
        )

    logger.info(
        "Polar retime: %d workouts moved, %d dropped as stale duplicates, %d blocked; "
        "%d RR beats moved, %d dropped as stale duplicates, %d left in place as ambiguous",
        len(ids) - workouts_dropped - workouts_blocked,
        workouts_dropped,
        workouts_blocked,
        beats_moved,
        beats_dropped,
        beats_ambiguous,
    )


def downgrade() -> None:
    # A no-op downgrade would let the next upgrade shift everything a second time.
    raise RuntimeError(
        "c6d2a8f4e1b3 retimed stored Polar data and cannot be reversed. To move the schema "
        "below it without re-running the shift, use `alembic stamp b4e8f2a7c915`."
    )
