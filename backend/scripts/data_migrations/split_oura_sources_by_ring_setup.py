#!/usr/bin/env python3
"""Re-attribute Oura history that was labelled with the wrong ring.

Oura's data endpoints carry no device, so the ring model came from
``/usercollection/ring_configuration`` via the connection's device_label. That
auto-detection reported whichever ring is set up *now* and then stamped the label
across every existing device-less data source, with no date bound. A user who
changed rings therefore has years of history attributed to hardware they did not
own yet — e.g. a source labelled "Oura Ring Or5" holding events from 2025-05-10
for a ring first worn 2026-07-16. (The ingestion side is fixed separately; this
script repairs rows already written.)

Every affected source *spans* its changeover, so the label can be neither kept nor
cleared wholesale: the fix is to split each source by date. Ring ownership comes
from each ring's own ``set_up_at``, so no changeover date is assumed:

    ring[i] owns [set_up_at[i], set_up_at[i+1])   the newest owns [set_up_at[-1], inf)
    anything older than the earliest ring is unattributable -> device_model NULL

Device-less sources are processed too, not just mislabelled ones. Time series used
to resolve their data source through a path that did not apply the connection's
device label, so a ring's HR/HRV could land on a device-less source while its sleep
events landed on the labelled one (fixed separately). The same windows re-attribute
those points, and because a window that already matches the source is skipped, data
genuinely predating every known ring stays device-less instead of being swept onto
the current ring.

Rows are moved, never deleted. A row that would collide with the target's unique
constraint is left where it is and counted as skipped, so the script is safe to
re-run: once moved, rows no longer match their source filter.

Requires the Oura API (it reads ring_configuration per user), so it is NOT part of
the startup sequence — run it manually.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/split_oura_sources_by_ring_setup.py
    docker compose exec app uv run python scripts/data_migrations/split_oura_sources_by_ring_setup.py --apply
"""

import argparse
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.schemas.enums import ProviderName, infer_device_type_from_model
from app.services.providers.oura.data_247 import Oura247Data
from app.services.providers.oura.strategy import OuraStrategy

PROVIDER = ProviderName.OURA.value
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
FOREVER = datetime(2999, 1, 1, tzinfo=timezone.utc)


def _rings(raw: dict) -> list[tuple[str, datetime]]:
    """[(device_model, set_up_at)] for every ring the account reports, oldest first."""
    out: list[tuple[str, datetime]] = []
    for ring in raw.get("data") or []:
        set_up_at, hardware = ring.get("set_up_at"), ring.get("hardware_type")
        if not set_up_at or not hardware:
            continue
        try:
            when = datetime.fromisoformat(set_up_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        parts = ["Oura Ring", hardware.replace("_", " ").title()]
        if ring.get("design"):
            parts.append(str(ring["design"]).replace("_", " ").title())
        out.append((" ".join(parts), when))
    return sorted(out, key=lambda r: r[1])


def _ownership(rings: list[tuple[str, datetime]]) -> list[tuple[str | None, datetime, datetime]]:
    """Half-open [start, end) windows naming the ring worn during each one."""
    windows: list[tuple[str | None, datetime, datetime]] = []
    if not rings:
        return windows
    # Anything before the first ring was set up cannot be attributed to any ring.
    windows.append((None, EPOCH, rings[0][1]))
    for i, (model, set_up_at) in enumerate(rings):
        end = rings[i + 1][1] if i + 1 < len(rings) else FOREVER
        windows.append((model, set_up_at, end))
    return windows


def _target_source(db: Session, user_id: UUID, device_model: str | None, source: str | None) -> UUID:
    """Find (or create) the data source for this user/model/source tag."""
    existing = db.execute(
        text("""
            SELECT id FROM data_source
            WHERE user_id = :user_id AND provider = :provider
              AND COALESCE(device_model, '') = COALESCE(:device_model, '')
              AND COALESCE(source, '') = COALESCE(:source, '')
            LIMIT 1
        """),
        {"user_id": user_id, "provider": PROVIDER, "device_model": device_model, "source": source},
    ).scalar()
    if existing:
        return existing

    new_id = uuid4()
    db.execute(
        text("""
            INSERT INTO data_source (id, user_id, provider, device_model, source, device_type, original_source_name)
            VALUES (:id, :user_id, :provider, :device_model, :source, :device_type, 'Oura')
            ON CONFLICT DO NOTHING
        """),
        {
            "id": new_id,
            "user_id": user_id,
            "provider": PROVIDER,
            "device_model": device_model,
            "source": source,
            "device_type": infer_device_type_from_model(device_model).value,
        },
    )
    return _target_source(db, user_id, device_model, source)


_MOVE_EVENTS = text("""
    UPDATE event_record er SET data_source_id = :target
    WHERE er.data_source_id = :src
      AND er.start_datetime >= :lo AND er.start_datetime < :hi
      AND NOT EXISTS (
          SELECT 1 FROM event_record e2
          WHERE e2.data_source_id = :target
            AND e2.start_datetime = er.start_datetime
            AND e2.end_datetime = er.end_datetime
      )
""")

_MOVE_POINTS = text("""
    UPDATE data_point_series d SET data_source_id = :target
    WHERE d.data_source_id = :src
      AND d.recorded_at >= :lo AND d.recorded_at < :hi
      AND NOT EXISTS (
          SELECT 1 FROM data_point_series d2
          WHERE d2.data_source_id = :target
            AND d2.series_type_definition_id = d.series_type_definition_id
            AND d2.recorded_at = d.recorded_at
      )
""")

_COUNT = text("""
    SELECT
      (SELECT COUNT(*) FROM event_record
         WHERE data_source_id = :src AND start_datetime >= :lo AND start_datetime < :hi),
      (SELECT COUNT(*) FROM data_point_series
         WHERE data_source_id = :src AND recorded_at >= :lo AND recorded_at < :hi)
""")


def run(apply: bool) -> None:
    oura_247 = OuraStrategy().data_247
    if not isinstance(oura_247, Oura247Data):  # pragma: no cover - strategy wiring guard
        raise RuntimeError("Oura strategy is missing its 247-data client")

    with SessionLocal() as db:
        sources = db.execute(
            text("""
                SELECT id, user_id, device_model, source
                FROM data_source
                WHERE provider = :provider
                ORDER BY user_id
            """),
            {"provider": PROVIDER},
        ).all()

        if not sources:
            print("No labelled Oura data sources — nothing to do.")
            return

        rings_by_user: dict[UUID, list[tuple[str, datetime]]] = {}
        moved_events = moved_points = 0

        for src_id, user_id, device_model, source_tag in sources:
            if user_id not in rings_by_user:
                try:
                    rings_by_user[user_id] = _rings(oura_247.get_ring_configuration(db, user_id))
                except Exception as exc:  # noqa: BLE001 - one user's token failing must not stop the rest
                    print(f"  ! {user_id}: could not read ring_configuration ({exc}); skipping")
                    rings_by_user[user_id] = []
            rings = rings_by_user[user_id]

            if not rings:
                continue

            print(f"\n{user_id}  source={device_model!r} tag={source_tag!r}")
            for model, lo, hi in _ownership(rings):
                if model == device_model:
                    continue  # already attributed correctly
                params = {"src": src_id, "lo": lo, "hi": hi}
                events, points = db.execute(_COUNT, params).one()
                if not events and not points:
                    continue

                window = f"{lo.date()} -> {'-' if hi == FOREVER else hi.date()}"
                print(f"  {window}: {events} events, {points} points -> {model or 'device_model NULL'}")
                if not apply:
                    continue

                target = _target_source(db, user_id, model, source_tag)
                moved_events += db.execute(_MOVE_EVENTS, {**params, "target": target}).rowcount
                moved_points += db.execute(_MOVE_POINTS, {**params, "target": target}).rowcount

        if apply:
            db.commit()
            print(f"\n✓ Moved {moved_events} event records and {moved_points} data points.")
            print("  (rows that would collide with an existing row were left in place)")
        else:
            print("\nDry run — nothing changed. Re-run with --apply to perform the move.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the move (default: dry run)")
    run(parser.parse_args().apply)
