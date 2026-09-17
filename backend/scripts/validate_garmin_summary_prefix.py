"""Check whether Garmin's summaryId prefix actually identifies a device.

Garmin reports ``deviceName`` only on activities. Every wellness summary it sends -
sleep, dailies, epochs, HRV, pulse ox, body composition, respiration, skin
temperature - carries no device field at all, which is why a user's Garmin sleep
and Garmin activities currently cannot be told apart by device.

Those summaries do carry a ``summaryId`` shaped
``{devicePrefix}-{hex(startTimeInSeconds)}`` with an optional third segment, and the
leading token is stable across a user's uploads. Whether it is stable *per device* -
which is what would make it a device identifier rather than an account one - is not
documented, and assuming wrongly would merge a user's watch and their scale into one
device with no visible symptom. So the registry records the prefix as a WEAK claim,
which can support a link proposal but never groups anything on its own.

This script answers the question from real data, using the summary ids already
stored in ``event_record.external_id``. Read it as:

- **Prefixes per user > 1, and each prefix's records concentrate in one data source
  or activity class** -> the prefix tracks a device. Set
  ``GARMIN_SUMMARY_PREFIX_VALIDATED = True`` in app/services/devices/identity.py and
  prefixes start grouping.
- **Exactly one prefix per user, spanning every data source** -> the prefix tracks
  the *account*, not a device. Leave the flag alone; grouping on it would merge
  every Garmin device a user owns.
- **A prefix that spans several device_models** -> definitely not per-device. Leave
  the flag alone.

Read-only unless ``--record`` is passed.

Usage (from ``backend/``)::

    uv run python scripts/validate_garmin_summary_prefix.py
    uv run python scripts/validate_garmin_summary_prefix.py --user <uuid> --verbose
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session


@dataclass
class PrefixStats:
    """What one summaryId prefix co-occurs with, across a user's records.

    A dataclass rather than a dict because the fields are not the same shape -
    three sets and a counter - and a dict annotation that says otherwise is either
    wrong or has to widen to the point of saying nothing.
    """

    data_sources: set[UUID] = field(default_factory=set)
    models: set[str | None] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    count: int = 0

    def add(self, data_source_id: UUID, device_model: str | None, category: str) -> None:
        self.data_sources.add(data_source_id)
        self.models.add(device_model)
        self.categories.add(category)
        self.count += 1

    @property
    def named_models(self) -> set[str]:
        """Models excluding the NULLs, which say nothing about which device this is."""
        return {m for m in self.models if m}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", help="restrict to one user id")
    parser.add_argument("--verbose", action="store_true", help="list every prefix, not just the summary")
    parser.add_argument(
        "--record",
        action="store_true",
        help="also write the prefixes as WEAK identity claims on each data source's device",
    )
    args = parser.parse_args(argv)

    # Imported here, not at module scope: importing app.* instantiates Settings, so a
    # top-level import would make even `--help` require a configured environment.
    from app.database import SessionLocal
    from app.services.devices.identity import garmin_summary_prefix

    session = SessionLocal()
    try:
        where = "ds.provider = 'garmin' AND er.external_id IS NOT NULL"
        params: dict[str, object] = {}
        if args.user:
            where += " AND ds.user_id = :user_id"
            params["user_id"] = UUID(args.user)

        rows = session.execute(
            sa.text(f"""
                SELECT ds.user_id, ds.id AS data_source_id, ds.device_model, er.external_id, er.category
                FROM event_record er
                JOIN data_source ds ON ds.id = er.data_source_id
                WHERE {where}
            """),
            params,
        ).fetchall()

        if not rows:
            print("No Garmin event records with an external_id. Nothing to validate yet.")
            return 0

        # prefix -> what it co-occurs with. If a prefix is per-device, its records
        # should concentrate in one data source and one device_model.
        by_user: dict[UUID, dict[str, PrefixStats]] = defaultdict(lambda: defaultdict(PrefixStats))
        skipped = 0
        for user_id, data_source_id, device_model, external_id, category in rows:
            prefix = garmin_summary_prefix(external_id)
            if prefix is None:
                skipped += 1
                continue
            by_user[user_id][prefix].add(data_source_id, device_model, category)

        _report(by_user, len(rows), skipped, verbose=args.verbose)

        if args.record:
            written = _record_claims(session, by_user)
            print(f"\nRecorded {written} weak summary-prefix claims.")
        return 0
    finally:
        session.close()


def _report(by_user: dict[UUID, dict[str, PrefixStats]], total_rows: int, skipped: int, verbose: bool) -> None:
    print(f"Scanned {total_rows} Garmin event records ({skipped} with no parseable summaryId prefix).\n")

    single_prefix_users = 0
    multi_model_prefixes = 0
    for user_id, prefixes in sorted(by_user.items(), key=lambda kv: str(kv[0])):
        if len(prefixes) == 1:
            single_prefix_users += 1
        print(f"user {user_id}: {len(prefixes)} prefix(es)")
        for prefix, info in sorted(prefixes.items(), key=lambda kv: -kv[1].count):
            models = info.named_models
            if len(models) > 1:
                multi_model_prefixes += 1
            if verbose or len(prefixes) > 1:
                print(
                    f"  {prefix:16} records={info.count:<7} "
                    f"data_sources={len(info.data_sources)} "
                    f"models={sorted(models) or ['-']} "
                    f"categories={sorted(info.categories)}"
                )
        print()

    users = len(by_user)
    print("-" * 72)
    print(f"users scanned:                      {users}")
    print(f"users with exactly one prefix:      {single_prefix_users}")
    print(f"prefixes spanning >1 device_model:  {multi_model_prefixes}")
    print("-" * 72)

    if multi_model_prefixes:
        print(
            "\nVERDICT: not per-device. At least one prefix spans several device models,\n"
            "so grouping on it would merge different hardware. Leave\n"
            "GARMIN_SUMMARY_PREFIX_VALIDATED = False."
        )
    elif users and single_prefix_users == users:
        print(
            "\nVERDICT: inconclusive, leaning per-account. Every user has exactly one\n"
            "prefix, which is what you would see either if each user owns one Garmin\n"
            "device or if the prefix identifies the account. Re-run once a user with\n"
            "two Garmin devices has synced. Leave the flag False until then."
        )
    else:
        print(
            "\nVERDICT: consistent with per-device. Some users have several prefixes and\n"
            "none spans more than one device model. Check above that each prefix's\n"
            "categories look like one device's workload before setting\n"
            "GARMIN_SUMMARY_PREFIX_VALIDATED = True."
        )


def _record_claims(session: Session, by_user: dict[UUID, dict[str, PrefixStats]]) -> int:
    """Write each prefix as a WEAK claim on the device behind its data sources."""
    from app.models import DataSource
    from app.repositories.device_repository import DeviceRepository
    from app.schemas.enums import DeviceIdentityKind, IdentityConfidence, ProviderName
    from app.services.devices.identity import IdentityClaim

    repo = DeviceRepository()
    written = 0
    for user_id, prefixes in by_user.items():
        for prefix, info in prefixes.items():
            claim = IdentityClaim(
                route=ProviderName.GARMIN.value,
                kind=DeviceIdentityKind.GARMIN_SUMMARY_PREFIX,
                value=prefix,
                confidence=IdentityConfidence.WEAK,
            )
            for data_source_id in info.data_sources:
                data_source = session.get(DataSource, data_source_id)
                if data_source is None or data_source.device_id is None:
                    continue
                device = repo.get(session, data_source.device_id)
                if device is None:
                    continue
                if repo.add_claim(session, device, claim, actor="script:validate_garmin_summary_prefix") is not None:
                    written += 1
                break
    session.commit()
    return written


if __name__ == "__main__":
    sys.exit(main())
