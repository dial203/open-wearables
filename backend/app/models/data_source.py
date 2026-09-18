from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Index, text
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import (
    FKDeviceOptional,
    FKUser,
    FKUserConnection,
    ManyToOne,
    OneToMany,
    PrimaryKey,
    str_32,
    str_50,
    str_100,
)
from app.schemas.enums import ProviderName

if TYPE_CHECKING:
    from app.models.data_point_series import DataPointSeries
    from app.models.device import Device
    from app.models.event_record import EventRecord


class DataSource(BaseDbModel):
    """Maps a user/provider/device combination into a reusable identifier.

    user_connection_id is NULL for one-time imports (XML, manual uploads),
    populated for active connections (SDK sync, OAuth API).
    """

    __tablename__ = "data_source"
    __table_args__ = (
        Index("ix_data_source_user_provider", "user_id", "provider"),
        Index(
            "uq_data_source_identity",
            "user_id",
            "provider",
            text("COALESCE(device_model, '')"),
            text("COALESCE(source, '')"),
            unique=True,
        ),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    provider: Mapped[ProviderName]
    user_connection_id: Mapped[FKUserConnection]
    device_model: Mapped[str_100 | None]
    software_version: Mapped[str_50 | None]
    # Apple HealthKit tags on-device data with source bundle ids like
    # "com.apple.health.<UUID>" (53 chars), so 50 is too short and aborts SDK
    # imports - see migration 264b79d7c541. Populated by ensure_data_source()
    # in app/repositories/data_source_repository.py. Apple documents no max
    # length for HKSource.bundleIdentifier (it is an app bundle id or a device
    # UUID, see https://developer.apple.com/documentation/healthkit/hksource/bundleidentifier);
    # 100 fits the observed identifiers and matches the other str_100 columns.
    source: Mapped[str_100 | None]
    device_type: Mapped[str_32 | None]
    original_source_name: Mapped[str_100 | None]

    # The physical unit this source is attributed to, once one is known. NULL means
    # unattributed, which is a normal resting state rather than an error: detection
    # only groups on provider-issued identifiers, so a source whose provider reports
    # none waits for a person to link it. ON DELETE SET NULL - removing a device must
    # never remove ingested data.
    #
    # This does not replace device_model/source. Those stay exactly as the provider
    # sent them, because they are the record of what the provider claimed; device_id
    # is our interpretation of it, and the two have to stay separable.
    device_id: Mapped[FKDeviceOptional]
    # When someone detached this source by hand. Detection then leaves it alone, and
    # linking it to a device again clears it.
    #
    # Without it a NULL device_id says only "no device", which detection reads as
    # "never attributed" - so it re-attaches on the next batch and a deliberate
    # detach silently undoes itself. Attribution is already write-once for a source
    # that *has* a device; this gives the empty state the same protection.
    attribution_locked_at: Mapped[datetime | None]
    # Not eager-loaded: attribution is read per sample on the timeseries paths, and a
    # lazy load there would be one query per row. Callers that want the device's label
    # join it explicitly; SourceMetadata falls back to the id alone when they have not.
    device: Mapped[ManyToOne["Device"]]

    event_records: Mapped[OneToMany["EventRecord"]]
    data_points: Mapped[OneToMany["DataPointSeries"]]
