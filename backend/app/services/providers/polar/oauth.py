import logging

import httpx

from app.config import settings
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)


class PolarOAuth(BaseOAuthTemplate):
    """Polar OAuth 2.0 implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://flow.polar.com/oauth2/authorization",
            token_url="https://polarremote.com/v2/oauth2/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.polar_client_id or "",
            client_secret=(settings.polar_client_secret.get_secret_value() if settings.polar_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.POLAR),
            default_scope=settings.polar_default_scope,
        )

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Extracts Polar user ID from token response and registers user."""
        raw = token_response.model_extra.get("x_user_id") if token_response.model_extra else None
        provider_user_id = str(raw) if raw is not None else None

        if provider_user_id:
            self._register_user(token_response.access_token, self._member_id(user_id, provider_user_id))

        return {"user_id": provider_user_id, "username": None}

    @staticmethod
    def _member_id(user_id: str, provider_user_id: str) -> str:
        """Our identifier for one Polar account, as AccessLink registers it.

        Polar refuses a second registration under a member-id it has already seen
        (409, "duplicated member-id"), so the user id alone only worked while a user
        could hold one Polar account. The second account's registration was refused,
        the refusal went unread, and every AccessLink read on that account then
        answered 403 - so it synced nothing at all, sleep included.
        """
        return f"{user_id}-{provider_user_id}"

    def _register_user(self, access_token: str, member_id: str) -> None:
        """Registers the user with Polar API.

        Never raises: the connection is still worth saving, and a reconnect retries.
        """
        try:
            register_url = f"{self.api_base_url}/v3/users"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            payload = {"member-id": member_id}

            response = httpx.post(register_url, json=payload, headers=headers, timeout=10.0)
        except Exception as e:
            log_structured(
                logger,
                "error",
                f"Polar user registration request failed: {e}",
                provider="polar",
                task="register_user",
                member_id=member_id,
            )
            return

        if response.status_code in (200, 201):
            log_structured(logger, "info", "Registered Polar user", provider="polar", member_id=member_id)
        elif response.status_code == 409:
            # Already registered with this client, which is what a reconnect looks like.
            log_structured(logger, "info", "Polar user already registered", provider="polar", member_id=member_id)
        else:
            # Unregistered, AccessLink answers 403 to every data request, and nothing
            # else says why an account syncs nothing. 403 here is Polar's "mandatory
            # consents not accepted", fixed by the account holder at account.polar.com.
            log_structured(
                logger,
                "error",
                f"Polar user registration refused ({response.status_code}): no data will sync for this account",
                provider="polar",
                task="register_user",
                member_id=member_id,
                status_code=response.status_code,
                response_body=response.text[:500],
            )

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Call Polar's user deregistration endpoint to remove the app association."""

        if not provider_user_id:
            raise ValueError("Polar deregistration requires provider_user_id")

        deregister_url = f"{self.api_base_url}/v3/users/{provider_user_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }
        response = httpx.delete(deregister_url, headers=headers, timeout=10.0)
        response.raise_for_status()
