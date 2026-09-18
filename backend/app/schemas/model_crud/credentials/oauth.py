from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# OAuth State (Redis)
class OAuthState(BaseModel):
    """OAuth state stored in Redis during authorization flow.

    The account fields are what make a second Garmin (or Whoop, or Oura) on the
    same participant possible. The provider's callback carries nothing but the
    code and this state, so whether the person is re-authorising an account they
    already have or adding another one has to be decided when the flow starts
    and carried through - a callback cannot work it out on its own.
    """

    user_id: UUID
    provider: str
    redirect_uri: str | None = None
    # Re-authorise this existing account rather than adding one. When set, the
    # callback refuses to touch any other connection.
    connection_id: UUID | None = None
    # Add a new account even though the user already has one with this provider.
    new_account: bool = False
    # Recorded on the connection the callback resolves, so a freshly added
    # account is named and traceable from the moment it exists rather than
    # showing up as an unlabelled duplicate.
    account_label: str | None = None
    account_email: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# OAuth Token Response
class OAuthTokenResponse(BaseModel):
    """OAuth token response from provider.

    Standard OAuth 2.0 fields are declared explicitly. Provider-specific extras
    (e.g. Polar's ``x_user_id``, Fitbit's ``user_id``) are captured automatically
    in ``model_extra`` thanks to ``extra='allow'``, so the schema stays clean as
    new providers are added.
    """

    model_config = ConfigDict(extra="allow")

    access_token: str
    token_type: str
    refresh_token: str | None = None
    expires_in: int
    scope: str | None = None


# Provider config
class ProviderEndpoints(BaseModel):
    """Static endpoints for an OAuth provider."""

    authorize_url: str
    token_url: str


class ProviderCredentials(BaseModel):
    """User-configurable credentials for an OAuth provider."""

    client_id: str
    client_secret: str
    redirect_uri: str
    default_scope: str
    subscription_key: str | None = None  # Suunto-specific


# Authorization URL response
class AuthorizationURLResponse(BaseModel):
    """Response containing authorization URL for user redirect."""

    authorization_url: str
    state: str


class Token(BaseModel):
    """JWT access token response."""

    access_token: str
    token_type: str
