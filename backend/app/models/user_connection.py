from datetime import datetime
from uuid import UUID

from sqlalchemy import Index
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_64, str_100
from app.schemas.auth import ConnectionStatus


class UserConnection(BaseDbModel):
    """OAuth connections to external cloud providers (Suunto, Garmin, Polar, Coros)"""

    __table_args__ = (
        Index(
            "ix_user_connection_token_expiry",
            "token_expires_at",
            postgresql_where="status = 'active'",
        ),
        Index("ix_user_connection_user_provider", "user_id", "provider", unique=True),
        Index("ix_user_connection_status_user_id", "status", "user_id"),
        Index(
            "ix_user_connection_provider_external_id",
            "provider",
            "provider_user_id",
            postgresql_where="provider_user_id IS NOT NULL AND status = 'active'",
        ),
    )
    __tablename__ = "user_connection"

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    provider: Mapped[str_64]  # 'suunto', 'garmin', 'polar', 'coros'

    # Provider user data
    provider_user_id: Mapped[str | None]
    provider_username: Mapped[str | None]

    # Device identifier for the hardware behind this connection (e.g. "Whoop 5.0",
    # "Oura Ring Gen3"). Filled in for providers whose API doesn't report a device
    # model: set manually via the API, or auto-derived (Oura ring_configuration).
    # ensure_data_source() uses it to populate data_source.device_model when the
    # provider passes None.
    device_label: Mapped[str_100 | None]

    # OAuth tokens (optional for SDK-based providers like Apple)
    access_token: Mapped[str | None]
    refresh_token: Mapped[str | None]
    token_expires_at: Mapped[datetime | None]
    scope: Mapped[str | None]

    # Metadata
    status: Mapped[ConnectionStatus]
    last_synced_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]
