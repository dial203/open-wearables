#!/usr/bin/env python3
"""Split devices that pooled several relayed writers behind one phone's model string.

On an aggregator route (Apple Health, Health Connect, Google Health, Samsung Health)
``device_model`` names the handset that *synced* a batch, not the device that recorded
it: HealthKit reports ``productType``, so a Muse headband, an Oura ring and a WHOOP
band relayed through one iPhone all arrive stamped "iPhone15,3". Detection grouped on
that model string, and the device-registry migration's own backfill created one device
per (user, provider, device_model) - so every app relaying through a phone collapsed
into a single device row. In one real account that was ~30 data sources and ~2,500
sleep sessions from nine brands reading as one unit.

That is the over-merge app/services/devices/detection.py exists to prevent, and it has
no visible symptom: "all data from this device" silently returns several devices' data,
and by the time it reaches an analysis the samples cannot be separated again.

Ingest now keys these routes on the writer/model *pair*
(DeviceIdentityKind.AGGREGATOR_WRITER_MODEL, see app/services/devices/identity.py).
This script applies the same rule to rows already attributed, building the claims the
same way ingest does so the repair cannot drift from it.

What it does, per user:

  * recompute each aggregator-route data source's grouping claim;
  * group sources that now share a claim onto one device, reusing the device that
    already holds that claim, else the source's current device when it is not being
    shared with another claim, else a new one;
  * move nothing on a non-aggregator route, and nothing a person has deliberately
    detached (attribution_locked_at);
  * drop the stale bare model_string identity claims on aggregator routes, since
    leaving them would re-pool the sources on the next sync;
  * record every move in device_history with this script as the actor.

Manual work outranks it: a device carrying a typed label, notes or a wear location is
never deleted, and a source whose latest ``linked`` history row was written by a person
is left exactly where they put it. Both cases are counted in the report.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/repair_aggregator_device_pooling.py
    docker compose exec app uv run python scripts/data_migrations/repair_aggregator_device_pooling.py --apply

Options:
    --user <uuid>     Limit to one user
    --provider apple  Limit to one aggregator route (default: all of them)
    --prune-empty     Also delete auto-created devices left with no data sources
"""

import argparse
from collections import Counter, defaultdict
from typing import TypedDict
from uuid import UUID

from sqlalchemy import select

from app.database import DbSession, SessionLocal
from app.models import DataSource, Device, DeviceHistory, DeviceIdentity
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import (
    WRITER_MODEL_SEPARATOR,
    DeviceHistoryAction,
    DeviceIdentityKind,
    DeviceType,
    LabelSource,
)
from app.services.devices.identity import WRITER_ID_ROUTES, IdentityClaim, claims_from_data_source
from app.utils.device_registry import humanize_device_model, resolve_brand

ACTOR = "script:repair_aggregator_device_pooling"

# (user_id, route, id_kind, id_value) - the key device_identity is unique on.
GroupKey = tuple[UUID, str, str, str]

# The routes the writer/model rule applies to - the same set identity.py pairs on.
# Strava is an aggregator too but never carries a writer id, so its sources still group
# on the model string and this script must leave them alone.
AGGREGATOR_ROUTES = WRITER_ID_ROUTES


def _grouping_claim(data_source: DataSource) -> IdentityClaim | None:
    """The claim ingest would group this source by today, or None if it has none."""
    provider = getattr(data_source.provider, "value", data_source.provider)
    claims = claims_from_data_source(provider, data_source.device_model, data_source.source)
    for kind in (DeviceIdentityKind.AGGREGATOR_WRITER_MODEL, DeviceIdentityKind.MODEL_STRING):
        for claim in claims:
            if claim.kind is kind:
                return claim
    return None


def _is_hand_made(device: Device) -> bool:
    """Whether a person shaped this device row, so the script must not delete it."""
    return bool(
        device.label_source == LabelSource.MANUAL.value or device.notes or device.wear_location or device.retired_at
    )


def _device_fields(claim: IdentityClaim, data_source: DataSource) -> dict:
    """Fields for a device this script has to create.

    The same choice detection makes: the provider's model string is kept verbatim,
    and on an aggregator route the writing app's name becomes an auto label, because
    the model there names the relaying handset and would put "iPhone 14 Pro Max" on a
    Muse headband.
    """
    provider = getattr(data_source.provider, "value", data_source.provider)
    fields = {
        "brand": resolve_brand(data_source.provider, data_source.device_model, data_source.source),
        "model_raw": data_source.device_model,
        "model_display": humanize_device_model(data_source.device_model),
        "label": None,
        "reason": f"Regrouped on the {provider} route",
    }
    if claim.kind is DeviceIdentityKind.AGGREGATOR_WRITER_MODEL:
        fields["label"] = claim.value.split(WRITER_MODEL_SEPARATOR, 1)[0] or None
        fields["reason"] = f"Split out of the {provider} model-string pool"
    return fields


def _hand_linked_sources(db: DbSession, sources: list[DataSource]) -> set[UUID]:
    """Data sources a person attached to the device they currently sit on.

    A human decision outranks this script. The question is not "who touched this last"
    - device_history rows written in one transaction share a timestamp and cannot be
    ordered - but "did a person put it where it is": a ``linked`` row by a human actor
    whose new_value is the source's current device_id.
    """
    current = {ds.id: ds.device_id for ds in sources if ds.device_id is not None}
    if not current:
        return set()

    rows = db.scalars(
        select(DeviceHistory).where(
            DeviceHistory.data_source_id.in_(current),
            DeviceHistory.action == DeviceHistoryAction.LINKED.value,
        )
    ).all()

    hand_linked: set[UUID] = set()
    for row in rows:
        source_id = row.data_source_id
        if source_id is None or row.actor is None:
            continue
        if row.actor.startswith(("system:", "script:")):
            continue
        if row.new_value == str(current[source_id]):
            hand_linked.add(source_id)
    return hand_linked


class RepairResult(TypedDict):
    """What the run actually did, for a caller that is not a terminal."""

    scanned: int
    groups: int
    moved: int
    created: int
    stale_claims_removed: int
    pruned: int
    skipped_locked: int
    skipped_hand_linked: int
    skipped_no_claim: int


def _result(
    scanned: int = 0,
    groups: int = 0,
    moved: int = 0,
    created: int = 0,
    stale_claims_removed: int = 0,
    pruned: int = 0,
    skipped_locked: int = 0,
    skipped_hand_linked: int = 0,
    skipped_no_claim: int = 0,
) -> RepairResult:
    return {
        "scanned": scanned,
        "groups": groups,
        "moved": moved,
        "created": created,
        "stale_claims_removed": stale_claims_removed,
        "pruned": pruned,
        "skipped_locked": skipped_locked,
        "skipped_hand_linked": skipped_hand_linked,
        "skipped_no_claim": skipped_no_claim,
    }


def run(
    db: DbSession,
    dry_run: bool,
    user_id: UUID | None = None,
    provider: str | None = None,
    prune_empty: bool = False,
) -> RepairResult:
    """Regroup one session's aggregator-route data sources. Prints its own report."""
    repo = DeviceRepository()
    routes = {provider} if provider else set(AGGREGATOR_ROUTES)
    unknown = routes - AGGREGATOR_ROUTES
    if unknown:
        raise SystemExit(f"not an aggregator route: {', '.join(sorted(unknown))}")

    stmt = select(DataSource).where(DataSource.provider.in_(sorted(routes)))
    if user_id:
        stmt = stmt.where(DataSource.user_id == user_id)
    sources = list(db.scalars(stmt).all())

    if not sources:
        print("No data sources on an aggregator route matched - nothing to do.")
        return _result()

    # --- plan -----------------------------------------------------------------
    # A claim maps to exactly one device per user, which is what the unique index on
    # device_identity enforces, so the group key is (user, route, kind, value).
    groups: dict[GroupKey, list[DataSource]] = defaultdict(list)
    claims: dict[GroupKey, IdentityClaim] = {}
    skipped_locked = 0
    skipped_no_claim = 0

    hand_linked = _hand_linked_sources(db, sources)
    skipped_hand_linked = 0

    for ds in sources:
        if ds.attribution_locked_at is not None:
            skipped_locked += 1
            continue
        if ds.id in hand_linked:
            skipped_hand_linked += 1
            continue
        claim = _grouping_claim(ds)
        if claim is None:
            skipped_no_claim += 1
            continue
        key = (ds.user_id, claim.route, claim.kind.value, claim.value)
        groups[key].append(ds)
        claims[key] = claim

    # Which groups currently sit on each device. A device serving more than one
    # group is a pool, and no single group may keep it - that is the merge being
    # undone.
    keys_by_device: dict[UUID, set[GroupKey]] = defaultdict(set)
    for key, group in groups.items():
        for ds in group:
            if ds.device_id is not None:
                keys_by_device[ds.device_id].add(key)

    plan: list[tuple[GroupKey, Device | None, list[DataSource]]] = []
    for key, group in groups.items():
        user, _route, _kind, _value = key
        target = repo.find_by_claim(db, user, claims[key])

        if target is None:
            # Reuse the group's current device when it is exclusively this group's:
            # nothing pooled there, so re-grouping would only churn ids and history.
            current = {ds.device_id for ds in group if ds.device_id is not None}
            if len(current) == 1:
                candidate = current.pop()
                if keys_by_device[candidate] == {key}:
                    target = db.get(Device, candidate)

        movers = [ds for ds in group if target is None or ds.device_id != target.id]
        if movers:
            plan.append((key, target, movers))

    print(f"Scanned {len(sources)} aggregator-route data source(s) in {len(groups)} group(s).")
    if skipped_locked:
        print(f"  {skipped_locked} skipped: deliberately detached (attribution_locked_at).")
    if skipped_hand_linked:
        print(f"  {skipped_hand_linked} skipped: a person linked this source to its device by hand.")
    if skipped_no_claim:
        print(f"  {skipped_no_claim} skipped: no grouping claim (provider reported no model).")

    total_moves = sum(len(movers) for _, _, movers in plan)
    to_create = sum(1 for _, target, _ in plan if target is None)
    print(f"  {total_moves} data source(s) to re-attribute; {to_create} new device(s) to create.\n")

    counts = {
        "scanned": len(sources),
        "groups": len(groups),
        "skipped_locked": skipped_locked,
        "skipped_hand_linked": skipped_hand_linked,
        "skipped_no_claim": skipped_no_claim,
    }

    if not plan:
        print("Every aggregator-route source is already grouped on its writer - nothing to do.")
        return _result(**counts)

    leaving: Counter[str] = Counter()
    for key, target, movers in sorted(plan, key=lambda item: (str(item[0][0]), item[0][3])):
        destination = f"device {target.id}" if target is not None else "a new device"
        print(f"  {key[3]}  ->  {destination}")
        for ds in movers:
            origin = db.get(Device, ds.device_id) if ds.device_id else None
            origin_label = (
                "unattributed" if origin is None else f"{origin.id} ({origin.model_raw or origin.label or '-'})"
            )
            leaving[origin_label] += 1
            print(
                f"      {ds.provider:<15} {str(ds.source or '-'):<28} "
                f"{str(ds.device_model or '-'):<12}  from {origin_label}"
            )

    print("\n  Sources leaving each device")
    for label, count in leaving.most_common():
        print(f"    {label}: {count}")

    if dry_run:
        print("\nPreview only - no changes made. Re-run with --apply to write them.")
        return _result(**counts, moved=total_moves, created=to_create)

    # --- apply ----------------------------------------------------------------
    created = 0
    for key, target, movers in plan:
        user, _route, _kind, _value = key
        claim = claims[key]
        group = groups[key]

        if target is None:
            fields = _device_fields(claim, group[0])
            reason = fields.pop("reason")
            target = repo.create(
                db,
                user_id=user,
                device_type=group[0].device_type or DeviceType.UNKNOWN.value,
                actor=ACTOR,
                reason=reason,
                detected=True,
                **fields,
            )
            created += 1

        repo.add_claim(db, target, claim, actor=ACTOR)
        for ds in movers:
            repo.attach_data_source(
                db,
                ds,
                target,
                actor=ACTOR,
                reason="Re-grouped on the writing app, not the relaying handset",
            )

    # A bare model-string claim that ingest no longer produces is what would re-pool
    # everything on the next sync, so it goes with the regrouping. Claims ingest
    # still produces stay: a source with no writer id (an export literal, a provider
    # key) legitimately groups on its model, and deleting those would only churn.
    still_issued = {(claim.route, claim.kind.value, claim.value) for claim in claims.values()}
    scanned_users = {ds.user_id for ds in sources}
    stale = [
        identity
        for identity in db.scalars(
            select(DeviceIdentity).where(
                DeviceIdentity.route.in_(sorted(routes)),
                DeviceIdentity.id_kind == DeviceIdentityKind.MODEL_STRING.value,
                DeviceIdentity.user_id.in_(scanned_users),
            )
        ).all()
        if (identity.route, identity.id_kind, identity.id_value) not in still_issued
    ]
    for identity in stale:
        db.delete(identity)
    db.flush()

    pruned = 0
    if prune_empty:
        for device in list(db.scalars(select(Device)).all()):
            if _is_hand_made(device) or repo.data_sources_for_device(db, device.id):
                continue
            repo.record(
                db,
                user_id=device.user_id,
                action=DeviceHistoryAction.UPDATED,
                field="deleted",
                old_value=str(device.id),
                actor=ACTOR,
                reason="Auto-created device left empty by the aggregator regrouping",
            )
            db.delete(device)
            pruned += 1

    db.commit()
    print(f"\nRe-attributed {total_moves} data source(s); created {created} device(s).")
    print(f"Removed {len(stale)} stale model-string claim(s) on aggregator routes.")
    if prune_empty:
        print(f"Deleted {pruned} empty auto-created device(s).")

    return _result(**counts, moved=total_moves, created=created, stale_claims_removed=len(stale), pruned=pruned)


def main(dry_run: bool, user_id: UUID | None, provider: str | None, prune_empty: bool) -> None:
    with SessionLocal() as db:
        run(db, dry_run=dry_run, user_id=user_id, provider=provider, prune_empty=prune_empty)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: preview only)")
    parser.add_argument("--user", type=UUID, help="Limit to a single user id")
    parser.add_argument("--provider", help="Limit to one aggregator route, e.g. apple")
    parser.add_argument(
        "--prune-empty",
        action="store_true",
        help="Delete auto-created devices left with no data sources (never one a person shaped)",
    )
    args = parser.parse_args()
    main(dry_run=not args.apply, user_id=args.user, provider=args.provider, prune_empty=args.prune_empty)
