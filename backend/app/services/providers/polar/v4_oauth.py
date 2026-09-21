"""OAuth for Polar's AccessLink v4 API.

v4 is the same AccessLink client — registered at the same admin panel, with the same
client id and secret — but a different authorization server (``auth.polar.com``) issuing
scoped tokens, and it has no user-registration step. The token it mints is not
interchangeable with a v3 token, which is why it lives on its own connection.
"""

from app.config import settings
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate

POLAR_V4_BASE_URL = "https://www.polaraccesslink.com"


class PolarV4OAuth(BaseOAuthTemplate):
    """Polar AccessLink v4 OAuth 2.0 implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://auth.polar.com/oauth/authorize",
            token_url="https://auth.polar.com/oauth/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.polar_client_id or "",
            client_secret=(settings.polar_client_secret.get_secret_value() if settings.polar_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.POLAR_V4),
            default_scope=settings.polar_v4_default_scope,
        )

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """v4 needs no user registration and returns no user id, so the connection carries none.

        The uniqueness guard on (user, provider, provider_user_id) is a partial index over
        non-NULL ids, so leaving it unset does not block the row.
        """
        raw = token_response.model_extra.get("x_user_id") if token_response.model_extra else None
        return {"user_id": str(raw) if raw is not None else None, "username": None}
