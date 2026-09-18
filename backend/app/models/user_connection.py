from datetime import datetime
from uuid import UUID

from sqlalchemy import Index, text
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKUser, PrimaryKey, str_32, str_64, str_100, str_255
from app.schemas.auth import ConnectionStatus


class UserConnection(BaseDbModel):
    """One external provider account linked to one Open Wearables user.

    A user may hold several connections to the same provider - two Garmin
    accounts worn simultaneously for a reliability study, say. Each row is one
    account, identified to a human by ``account_label`` and to an auditor by
    ``account_email``, and every data source ingested through it carries its id
    (``data_source.user_connection_id``), so samples never pool across accounts.
    """

    __table_args__ = (
        Index(
            "ix_user_connection_token_expiry",
            "token_expires_at",
            postgresql_where="status = 'active'",
        ),
        # Non-unique: one user may hold several accounts with the same provider.
        # The uniqueness that remains is per external account, below.
        Index("ix_user_connection_user_provider", "user_id", "provider"),
        Index("ix_user_connection_status_user_id", "status", "user_id"),
        # "every validation account", across participants - a study-wide filter.
        Index("ix_user_connection_account_type", "account_type"),
        # The same external account must not be linked twice to the same user:
        # two rows for one Whoop login would double-count every sample and make
        # the sync fan-out race itself. Providers that report no user id fall
        # through to the e-mail guard below.
        Index(
            "uq_user_connection_user_provider_account",
            "user_id",
            "provider",
            "provider_user_id",
            unique=True,
            postgresql_where="provider_user_id IS NOT NULL AND status = 'active'",
        ),
        Index(
            "uq_user_connection_user_provider_email",
            "user_id",
            "provider",
            text("lower(account_email)"),
            unique=True,
            postgresql_where="account_email IS NOT NULL AND status = 'active'",
        ),
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

    # What this account is for: personal, validation, reliability, monitoring,
    # testing, other (app/schemas/enums/account_type.py). The e-mail below says
    # *which* account this is; this says why it exists, which is what a study
    # filters on - the validation arm separated from the participant's own
    # everyday wear, the checkout account excluded from the analysis.
    #
    # NULL means nobody has classified it. Deliberately not defaulted: a wrong
    # classification that nobody chose is worse than a visibly empty one, and
    # the API reports it as unclassified so it can be found and fixed.
    #
    # Plain string rather than a database enum, matching device_type and
    # provider, so the set can grow without a migration.
    account_type: Mapped[str_32 | None]

    # Operator-facing free-text name, e.g. "P01 arm A". Optional, and never
    # asked of a participant - the classification above is the structured field.
    # Nothing enforces uniqueness, because a name is a note, not a key. When it
    # is NULL the API falls back to provider_username / account_email / a short
    # form of the id, so a connection is never unnamed on screen.
    account_label: Mapped[str_100 | None]

    # The login e-mail of the provider account behind this connection. Recorded
    # so a data set can always be traced back to the account it came from, which
    # for validation work is the difference between two devices and one. Captured
    # from the provider when its API exposes it, otherwise entered by hand at
    # connect time.
    account_email: Mapped[str_255 | None]

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
