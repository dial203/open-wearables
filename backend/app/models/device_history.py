from uuid import UUID

from sqlalchemy import Index
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import (
    FKDataSourceOptional,
    FKDeviceOptional,
    FKUser,
    PrimaryKey,
    json_object,
    str_32,
    str_64,
    str_128,
)


class DeviceHistory(BaseDbModel):
    """Append-only record of every change to device attribution.

    Nothing here is ever updated or deleted. Device attribution decides which
    physical unit a sample is credited to, so a relabel months after ingest silently
    rewrites the provenance of historical data unless the previous state is recoverable.
    This is what makes that recoverable, and what answers "why is this sample attributed
    to this device" without guesswork.

    ``device_id`` and ``data_source_id`` are ON DELETE SET NULL rather than CASCADE:
    a row describing a device that has since been merged away or deleted is exactly
    the row you need when reconstructing what happened, so it has to outlive its subject.
    The ids also stay in ``meta`` for that case, where they are plain values with no FK.
    """

    __tablename__ = "device_history"
    __table_args__ = (
        Index("ix_device_history_user_created", "user_id", "created_at"),
        Index("ix_device_history_device_created", "device_id", "created_at"),
        Index("ix_device_history_data_source", "data_source_id"),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    device_id: Mapped[FKDeviceOptional]
    data_source_id: Mapped[FKDataSourceOptional]

    action: Mapped[str_32]  # DeviceHistoryAction
    field: Mapped[str_64 | None]  # which attribute changed, for UPDATED
    old_value: Mapped[str | None]
    new_value: Mapped[str | None]

    # Who made the change: an API key label, a developer's email, or "system" for
    # auto-detection. Free text rather than an FK - the actor may be a process, and an
    # audit row must not fail to write because the actor could not be resolved.
    actor: Mapped[str_128 | None]
    reason: Mapped[str | None]

    # Anything the action needs that has no column: the absorbed device's id and label
    # on a merge, the moved data source ids on a split, the score and evidence on an
    # accepted proposal.
    meta: Mapped[json_object | None]
