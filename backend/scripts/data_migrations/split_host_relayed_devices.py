#!/usr/bin/env python3
"""Split devices that were built from the phone that relayed their data.

A third-party app writing into HealthKit or Health Connect generally passes no device
record of its own, so the platform reports the phone that ran the app. The Muse app on
an iPhone sends ``iPhone 17 Pro`` as the model for data a headband recorded - and so
does the Oura app, the WHOOP app and every other writer on that handset.

Detection used to group within a route on exactly that string, so all of them landed
on one device called ``iPhone 17 Pro``: an over-merge produced by the rule meant to
prevent one. Editing that device renamed every brand behind it at once, which is what
made the registry feel unusable for relayed data.

Detection now keys those sources on their writer instead (see
app/services/devices/identity.relaying_host_model). This script applies the same rule
to devices already created, so an existing database ends up where a fresh one would:

- one device per writing app, carrying that app's data sources;
- ``model_raw`` cleared, because the provider never reported this unit's hardware,
  and the phone moved to ``host_model_raw`` where it reads as provenance;
- the host's model-string and productType claims removed, since they describe the
  handset and would otherwise re-collide on the next sync;
- the writer's claim moved to the device it now identifies.

Nothing is merged and no data source is detached: every source keeps its samples and
lands on exactly one device. Every change is written to device_history, so a split
that turns out to be wrong is visible and can be merged back by hand.

The brand, type and name the new devices get are a starting point, not an answer - the
writing app's name is all the platform gave us. Expect to correct them in the UI; that
is what model_display, brand_display and the label are for.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/split_host_relayed_devices.py
    docker compose exec app uv run python scripts/data_migrations/split_host_relayed_devices.py --apply

Options:
    --user <uuid>   Limit to one user
"""

import argparse
from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DataSource, Device, DeviceIdentity
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import (
    WRITER_IDENTITY_KINDS,
    DeviceHistoryAction,
    DeviceIdentityKind,
    LabelSource,
    ProviderName,
    infer_device_type_from_source_name,
)
from app.services.devices.identity import relaying_host_model
from app.utils.device_registry import relayed_brand

ACTOR = "script:split_host_relayed_devices"

# Claims that describe the relaying handset rather than the device they sit on. They
# are removed rather than moved: there is no device here they are true of, and leaving
# one behind means the next sync's identical claim collides with it and raises a link
# proposal for a pair that is not a pair.
_HOST_CLAIM_KINDS = frozenset({DeviceIdentityKind.MODEL_STRING, DeviceIdentityKind.APPLE_PRODUCT_TYPE})


def _provider_enum(provider: str) -> ProviderName:
    try:
        return ProviderName(provider)
    except ValueError:
        return ProviderName.UNKNOWN


def _writer_of(data_source: DataSource) -> str | None:
    """The host model this source relays through, keyed by its writing app."""
    provider = getattr(data_source.provider, "value", data_source.provider)
    if relaying_host_model(provider, data_source.device_model, data_source.source) is None:
        return None
    return data_source.source


def _reclassify(
    db: Session, repo: DeviceRepository, device: Device, writer: str | None, host_model: str, provider: str
) -> None:
    """Rewrite a device that described a handset so it describes the relayed unit.

    Writes one history row per field. These columns are outside EDITABLE_FIELDS - they
    are the record of what a provider reported, and the API is right to refuse them -
    but this is the case where the record itself was filed against the wrong device,
    and leaving it would leave that assertion standing.
    """
    changes = {
        "model_raw": None,
        "model_display": None,
        "host_model_raw": host_model,
        "brand": relayed_brand(_provider_enum(provider), writer),
        "device_type": infer_device_type_from_source_name(writer).value,
    }
    if not device.label:
        changes["label"] = writer
    for field, new_value in changes.items():
        old_value = getattr(device, field)
        if old_value == new_value:
            continue
        setattr(device, field, new_value)
        repo.record(
            db,
            user_id=device.user_id,
            device_id=device.id,
            action=DeviceHistoryAction.UPDATED,
            field=field,
            old_value=None if old_value is None else str(old_value),
            new_value=None if new_value is None else str(new_value),
            actor=ACTOR,
            reason=f"Model named the relaying host {host_model}, not this device",
        )
    if device.label and device.label_source != LabelSource.MANUAL.value:
        device.label_source = LabelSource.AUTO.value


def _move_writer_claim(
    db: Session, repo: DeviceRepository, source_device: Device, target: Device, writer: str | None
) -> None:
    """Point the writing app's claim at the device it now identifies."""
    if not writer or source_device.id == target.id:
        return
    claim = db.scalars(
        select(DeviceIdentity).where(
            DeviceIdentity.device_id == source_device.id,
            DeviceIdentity.id_value == writer,
            DeviceIdentity.id_kind.in_([k.value for k in WRITER_IDENTITY_KINDS]),
        )
    ).first()
    if claim is None:
        return
    claim.device_id = target.id
    repo.record(
        db,
        user_id=target.user_id,
        device_id=target.id,
        action=DeviceHistoryAction.IDENTITY_ADDED,
        field=claim.id_kind,
        new_value=claim.id_value,
        actor=ACTOR,
        reason=f"Moved from device {source_device.id}: it identifies this writer's device",
        meta={"route": claim.route, "confidence": claim.confidence, "moved_from": str(source_device.id)},
    )


def _drop_host_claims(db: Session, repo: DeviceRepository, device: Device, host_model: str) -> int:
    """Remove the claims that described the handset. Returns how many went."""
    claims = db.scalars(select(DeviceIdentity).where(DeviceIdentity.device_id == device.id)).all()
    removed = 0
    for claim in claims:
        if DeviceIdentityKind(claim.id_kind) not in _HOST_CLAIM_KINDS:
            continue
        repo.record(
            db,
            user_id=device.user_id,
            device_id=device.id,
            action=DeviceHistoryAction.IDENTITY_REMOVED,
            field=claim.id_kind,
            old_value=claim.id_value,
            actor=ACTOR,
            reason=f"Described the relaying host {host_model}, not this device",
            meta={"route": claim.route, "confidence": claim.confidence},
        )
        db.delete(claim)
        removed += 1
    return removed


def run(db: Session, dry_run: bool, user_id: UUID | None = None) -> int:
    """Rework every device that was built from a relaying host. Returns how many."""
    repo = DeviceRepository()

    stmt = select(Device)
    if user_id:
        stmt = stmt.where(Device.user_id == user_id)
    devices = list(db.scalars(stmt).all())

    planned = 0
    for device in devices:
        sources = repo.data_sources_for_device(db, device.id)
        by_writer: dict[str | None, list[DataSource]] = defaultdict(list)
        direct: list[DataSource] = []
        host_models: set[str] = set()
        provider = ""
        for ds in sources:
            ds_provider = getattr(ds.provider, "value", ds.provider)
            host = relaying_host_model(ds_provider, ds.device_model, ds.source)
            if host is None:
                direct.append(ds)
                continue
            by_writer[ds.source].append(ds)
            host_models.add(host)
            provider = ds_provider

        if not by_writer:
            continue

        host_model = sorted(host_models)[0]
        writers = sorted(by_writer, key=lambda w: (w is None, w or ""))

        # Already reworked, or created by detection after the fix: one writer, nothing
        # the provider described, and the host recorded where it belongs. The test is on
        # the device rather than a marker because a data source never stops relaying -
        # its device_model still names the phone - so "relayed" alone would re-fire every
        # run and split a device off itself.
        if len(writers) == 1 and not direct and device.host_model_raw == host_model and device.model_raw is None:
            continue
        print(f"\ndevice {device.id} — {device.brand or '?'} {device.model_raw or '?'} ({device.device_type})")
        for writer in writers:
            print(f"    writer {writer or 'unnamed':<28} {len(by_writer[writer])} data source(s)")
        if direct:
            print(f"    keeping {len(direct)} source(s) whose provider named real hardware")
        planned += 1

        if dry_run:
            continue

        # The device keeps the first writer's sources; every other writer moves to its
        # own. A device that also holds sources the provider did describe keeps those
        # instead, and all of its relayed writers move out, so the two never mix.
        stays = writers[0] if not direct else None
        for writer in writers:
            if writer == stays:
                continue
            moved_ids = [ds.id for ds in by_writer[writer]]
            new_device = repo.split(db, device, moved_ids, actor=ACTOR, reason=f"Relayed by {writer} on {host_model}")
            _reclassify(db, repo, new_device, writer, host_model, provider)
            _move_writer_claim(db, repo, device, new_device, writer)
            _drop_host_claims(db, repo, new_device, host_model)

        if stays is not None:
            _reclassify(db, repo, device, stays, host_model, provider)
        if not direct:
            _drop_host_claims(db, repo, device, host_model)
        # The session runs without autoflush, and the next device's lookups have to see
        # the claims this one moved and removed.
        db.flush()

    return planned


def main(dry_run: bool, user_id: UUID | None) -> None:
    with SessionLocal() as db:
        planned = run(db, dry_run, user_id)

        if planned == 0:
            print("No device was built from a relaying host — nothing to do.")
            return

        if dry_run:
            print(f"\nPreview only — {planned} device(s) would be reworked. Re-run with --apply.")
            return

        db.commit()
        print(f"\nReworked {planned} device(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: preview only)")
    parser.add_argument("--user", type=UUID, help="Limit to a single user id")
    args = parser.parse_args()
    main(dry_run=not args.apply, user_id=args.user)
