from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Index
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKUser, OneToMany, PrimaryKey, str_32, str_64, str_100

if TYPE_CHECKING:
    from app.models.device_identity import DeviceIdentity


class Device(BaseDbModel):
    """One physical device belonging to a user, across every route its data arrives by.

    ``data_source`` stays the immutable ingest fingerprint - one row per
    (user, provider, device_model, source) as the provider reported it. ``device``
    is the mutable, human-facing entity several data sources point at, which is what
    lets a Garmin's activities, sleep and epoch streams read as one fenix, and an Oura
    ring arriving both from Oura's API and relayed through Apple Health read as one ring.

    Linking never merges the data sources behind it. The aggregator hop changes
    freshness, rounding and completeness, so "this ring, direct vs via Apple Health"
    stays a comparison you can run; the device only groups them.

    Everything here is editable by hand. Auto-detection proposes, a person decides,
    and every change is recorded in device_history.
    """

    __tablename__ = "device"
    __table_args__ = (
        Index("ix_device_user_id", "user_id"),
        Index("ix_device_user_brand_type", "user_id", "brand", "device_type"),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]

    # Canonical brand from resolve_brand() - "Oura", "Garmin", "Apple". Groups the
    # same maker regardless of the route the data took.
    brand: Mapped[str_64 | None]
    # The strongest raw model string seen for this device, exactly as a provider sent
    # it. Never normalized in place: it is the experimental record of what hardware
    # produced the samples, and display normalization belongs in model_display.
    #
    # NULL on a device detected from a relayed stream whose only model string named the
    # phone that relayed it (see host_model_raw). The provider never reported this
    # unit's hardware, and writing the host's model here would assert that it did.
    model_raw: Mapped[str_100 | None]
    # Marketing name for model_raw where we can name it, else NULL and callers fall
    # back to model_raw. Display only, and the field to correct by hand when a
    # provider's model string is wrong or absent.
    model_display: Mapped[str_100 | None]
    # Hand-set brand, for the same reason model_display exists: a relayed stream often
    # resolves to the platform's brand rather than the maker's, and brand itself is the
    # record of what was derived from the provider's report. Display only.
    brand_display: Mapped[str_64 | None]

    # Hand-entered inventory fields. No provider in the system reports either, so both
    # are always a person's assertion about hardware in front of them.
    #
    # serial is NOT an identity claim and groups nothing: DeviceIdentityKind.SERIAL is
    # for a serial a provider issued, and detection never reads this column. Firmware
    # is worth recording because it changes a device's behaviour without changing any
    # identifier - a validation run spanning a firmware update is comparing two things.
    serial: Mapped[str_100 | None]
    firmware_version: Mapped[str_64 | None]

    # The aggregator's host, for a device whose data reached us relayed: the phone that
    # ran the writing app, exactly as the platform reported it. A third-party app
    # writing into HealthKit generally passes no HKDevice, so the only model string on
    # those rows names the phone. That is real provenance and worth keeping, but it is
    # not this device's model, and keeping it in its own column is what stops every app
    # on one phone being grouped as that phone.
    host_model_raw: Mapped[str_100 | None]

    # DeviceType value. Stored as a string rather than the `devicetype` PG enum that
    # device_type_priority uses, matching data_source.device_type: adding a type is
    # then a code change, not an enum migration (see migration e2b7a4c1f8d3, which had
    # to alter the enum to add chest_strap).
    device_type: Mapped[str_32]

    # What a human calls this device ("Sub 04 fenix", "left-wrist Verity"). Free text.
    label: Mapped[str_100 | None]
    # LabelSource. A manual label is never overwritten by detection - a confidently
    # wrong auto-label is worse than no label, because nothing signals it is wrong.
    label_source: Mapped[str_32]

    # Study/protocol context that has no provider equivalent.
    wear_location: Mapped[str_32 | None]
    notes: Mapped[str | None]

    # is_active is the current state; retired_at is when it stopped being worn, which
    # is what separates pre- and post-replacement samples for an identical replacement
    # unit. A warranty swap reports the same brand, model and routes, so nothing but
    # this date distinguishes the two units.
    is_active: Mapped[bool]
    retired_at: Mapped[datetime | None]

    first_seen_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]

    updated_at: Mapped[datetime]

    identities: Mapped[OneToMany["DeviceIdentity"]]
