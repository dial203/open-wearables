#!/usr/bin/env python3
"""Narrow existing Strava model-string claims to the account they came from.

Detection groups data sources within a route on an exact model-string match, which
"reproduces what the provider already asserts". On Strava that assertion holds for one
connected account and stops holding for several: a validation study running an account
per wearable, with two devices of the same model, gets one device row with two units'
data pooled into it and nothing on screen to say so.

Detection now scopes the Strava model key to the account (see
identity.ACCOUNT_SCOPED_MODEL_ROUTES). Without this script the change is not wrong,
just noisy: the next sync's scoped key matches nothing, so every Strava device gains a
duplicate beside it that somebody merges by hand. This rewrites the claims already
stored so an existing database ends up where a fresh one would.

Only unambiguous claims are rewritten - a device whose Strava sources all arrived
through one connection. A device whose sources span two accounts is left alone and
reported: that is either one unit genuinely shared between two logins, or the
over-merge this scoping exists to prevent, and only a person can tell which. Splitting
it here would guess.

Nothing is created, deleted or detached: this edits claim values in place, and every
edit is written to device_history.

Usage (inside Docker):
    docker compose exec app uv run python scripts/data_migrations/scope_strava_model_claims_by_account.py
    docker compose exec app uv run python scripts/data_migrations/scope_strava_model_claims_by_account.py --apply

Options:
    --user <uuid>   Limit to one user
"""

import argparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import DataSource, DeviceIdentity, UserConnection
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import (
    WRITER_MODEL_SEPARATOR,
    DeviceHistoryAction,
    DeviceIdentityKind,
    ProviderName,
)

ACTOR = "script:scope_strava_model_claims_by_account"

ROUTE = ProviderName.STRAVA.value


def _account_scope(db: Session, device_id: UUID) -> str | None:
    """The one account every Strava source on this device arrived through, or None.

    None covers both cases that must not be rewritten: sources spanning two accounts,
    where the scope is genuinely ambiguous, and sources carrying no connection at all,
    which predate connections being recorded and have nothing to scope to.
    """
    connection_ids = set(
        db.scalars(
            select(DataSource.user_connection_id).where(
                DataSource.device_id == device_id,
                DataSource.provider == ROUTE,
            )
        )
    )
    if len(connection_ids) != 1:
        return None
    connection_id = connection_ids.pop()
    if connection_id is None:
        return None
    connection = db.get(UserConnection, connection_id)
    if connection is None:
        return None
    # The provider's own id for the account, not the row's, so the scope survives a
    # disconnect and reconnect - detection derives it the same way.
    return connection.provider_user_id or str(connection.id)


def run(db: Session, dry_run: bool, user_id: UUID | None) -> tuple[int, int]:
    """Returns (rewritten, skipped)."""
    repo = DeviceRepository()

    query = select(DeviceIdentity).where(
        DeviceIdentity.route == ROUTE,
        DeviceIdentity.id_kind == DeviceIdentityKind.MODEL_STRING.value,
    )
    if user_id is not None:
        query = query.where(DeviceIdentity.user_id == user_id)

    rewritten = 0
    skipped = 0
    for claim in db.scalars(query).all():
        if WRITER_MODEL_SEPARATOR in claim.id_value:
            # Already scoped, or a value that was never the bare model. Idempotent
            # so the script can be re-run after a partial apply.
            continue

        scope = _account_scope(db, claim.device_id)
        if scope is None:
            skipped += 1
            print(f"  skip  device={claim.device_id} value={claim.id_value!r} (no single account)")
            continue

        new_value = f"{claim.id_value}{WRITER_MODEL_SEPARATOR}{scope}"
        # The column is 255 and the scope is an athlete id, so this is headroom
        # rather than a real case - but a silently truncated key groups the wrong
        # things, which is worse than leaving the claim as it is.
        if len(new_value) > 255:
            skipped += 1
            print(f"  skip  device={claim.device_id} value={claim.id_value!r} (scoped key too long)")
            continue

        print(f"  scope device={claim.device_id} {claim.id_value!r} -> {new_value!r}")
        rewritten += 1
        if dry_run:
            continue

        old_value = claim.id_value
        claim.id_value = new_value
        repo.record(
            db,
            user_id=claim.user_id,
            device_id=claim.device_id,
            action=DeviceHistoryAction.IDENTITY_ADDED,
            field="id_value",
            old_value=old_value,
            new_value=new_value,
            actor=ACTOR,
            reason="Strava model strings now group per account, not per user",
        )
        db.flush()

    return rewritten, skipped


def main(dry_run: bool, user_id: UUID | None) -> None:
    with SessionLocal() as db:
        rewritten, skipped = run(db, dry_run, user_id)

        if rewritten == 0 and skipped == 0:
            print("No unscoped Strava model claims — nothing to do.")
            return

        if skipped:
            print(
                f"\n{skipped} claim(s) left alone: their device's sources span more than one "
                "Strava account, or none. Review those devices by hand - each is either one "
                "unit shared between two logins, or two units already pooled into one row."
            )

        if dry_run:
            print(f"\nPreview only — {rewritten} claim(s) would be scoped. Re-run with --apply.")
            return

        db.commit()
        print(f"\nScoped {rewritten} claim(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: preview only)")
    parser.add_argument("--user", type=UUID, help="Limit to a single user id")
    args = parser.parse_args()
    main(dry_run=not args.apply, user_id=args.user)
