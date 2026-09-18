from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_validator

from app.schemas.auth import ConnectionStatus, LiveSyncMode


class UserConnectionBase(BaseModel):
    """Base schema for UserConnection."""

    user_id: UUID
    provider: str
    provider_user_id: str | None = None
    provider_username: str | None = None
    account_label: str | None = None
    account_email: str | None = None
    scope: str | None = None


class UserConnectionCreate(UserConnectionBase):
    """Schema for creating a new UserConnection."""

    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    device_label: str | None = None
    access_token: str | None = None  # Optional for SDK-based providers (e.g., Apple)
    refresh_token: str | None = None
    token_expires_at: datetime | None = None  # Optional for SDK-based providers
    status: ConnectionStatus = ConnectionStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UserConnectionUpdate(BaseModel):
    """Schema for updating UserConnection."""

    model_config = ConfigDict(populate_by_name=True)

    access_token: str | None = None
    refresh_token: str | None = None
    token_expires_at: datetime | None = None
    provider_user_id: str | None = None
    provider_username: str | None = None
    account_label: str | None = None
    account_email: str | None = None
    device_label: str | None = None
    scope: str | None = None
    status: ConnectionStatus | None = None
    last_synced_at: datetime | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UserConnectionRead(UserConnectionBase):
    """Schema for reading UserConnection (without sensitive tokens)."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    device_label: str | None = None
    status: ConnectionStatus
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_label(self) -> str:
        """A name for this account that is never empty.

        Falls back through what the provider told us before giving up and using
        the id, so a connection is always identifiable on screen and in an
        export even when nobody has named it yet.
        """
        return account_display_label(
            self.account_label,
            self.provider_username,
            self.account_email,
            self.provider,
            self.id,
        )


def account_display_label(
    account_label: str | None,
    provider_username: str | None,
    account_email: str | None,
    provider: str,
    connection_id: UUID,
) -> str:
    """Best available human name for a provider account."""
    for candidate in (account_label, provider_username, account_email):
        if candidate and candidate.strip():
            return candidate.strip()
    return f"{provider} ({str(connection_id)[:8]})"


class UserConnectionAccountUpdate(BaseModel):
    """Editable, human-facing fields of one connected provider account.

    Only the fields present in the request body are touched; sending ``null``
    explicitly clears one. That distinction matters here because the e-mail is
    the record of which account a data set came from, and a partial update of an
    unrelated field must not be able to erase it.
    """

    model_config = ConfigDict(extra="forbid")

    account_label: str | None = Field(None, max_length=100, description="Human name for this account")
    account_email: EmailStr | None = Field(None, description="Login e-mail of the provider account")
    device_label: str | None = Field(None, max_length=100, description='Device behind this account, e.g. "Whoop 5.0"')

    @field_validator("account_label", "device_label")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        """An all-whitespace label is an unset label, not a label made of spaces."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class UserConnectionWithCapabilities(UserConnectionRead):
    """UserConnectionRead enriched with provider capability metadata.

    Extra fields are populated by the endpoint, not from the ORM model.
    """

    icon_url: str | None = Field(
        None,
        description=(
            "Relative URL to provider icon (e.g., '/static/provider-icons/garmin.svg')."
            " Resolve against the API base URL."
        ),
    )
    max_historical_days: int | None = None
    rest_pull: bool = False
    webhook_stream: bool = False
    webhook_ping: bool = False
    webhook_callback: bool = False
    live_sync_mode: LiveSyncMode | None = None
    linked_user_ids: list[UUID] = Field(default_factory=list)
    # 1-based position among this user's accounts with the same provider, oldest
    # first. Stable for as long as the account exists, and what the UI uses to
    # say "Garmin account 2" before anyone has named it.
    account_index: int = 1
    account_count: int = 1
