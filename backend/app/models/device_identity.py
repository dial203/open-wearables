from datetime import datetime
from uuid import UUID

from sqlalchemy import Index
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKDevice, FKUser, PrimaryKey, str_32, str_48, str_255


class DeviceIdentity(BaseDbModel):
    """One identity claim a single ingest route makes about a device.

    There is no identifier shared across routes for the same physical unit, so a
    device accumulates one claim per route it arrives through rather than carrying a
    single ``external_device_id``. An Oura ring seen three ways holds an
    ``oura_config_id`` claim on the oura route, a ``health_connect_package`` claim on
    the google route and a ``healthkit_bundle`` claim on the apple route; none of the
    three values resembles the others, and that is expected.

    ``confidence`` decides what a claim may do. A STRONG claim is provider-issued,
    stable for the life of the unit, and distinguishes two same-model devices owned by
    one user - only those group data sources automatically. A WEAK claim names the
    writing app or the hardware model, which two identical units share, so it can
    support a link proposal but never create a grouping on its own.
    """

    __tablename__ = "device_identity"
    __table_args__ = (
        # One claim value maps to at most one device per user. This is the lookup that
        # makes detection idempotent: the same ring re-syncing hits the existing row.
        # Scoped to the user, not global, because these values are only unique within
        # an account (two users' Health Connect package names are identical).
        Index("uq_device_identity_claim", "user_id", "route", "id_kind", "id_value", unique=True),
        Index("ix_device_identity_device_id", "device_id"),
    )

    id: Mapped[PrimaryKey[UUID]]
    # Denormalized from device so the uniqueness above is a plain index rather than a
    # join, and so detection can resolve a claim before touching the device row.
    user_id: Mapped[FKUser]
    device_id: Mapped[FKDevice]

    # ProviderName value of the route that issued this claim. Two routes can report
    # the same id_kind (both Apple and Google relay an Oura package id) and the values
    # are not interchangeable, so the route is part of the key.
    route: Mapped[str_32]
    id_kind: Mapped[str_48]  # DeviceIdentityKind
    id_value: Mapped[str_255]
    confidence: Mapped[str_32]  # IdentityConfidence

    first_seen_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime | None]
