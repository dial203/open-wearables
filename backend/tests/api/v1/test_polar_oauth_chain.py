"""Connecting Polar asks for the AccessLink v4 grant first, in the same click.

The order is the point. Connecting Polar starts a historical sync straight away, and that
sync is what reads v4's exact RR; a backfill that runs before the v4 token exists stores
v3's reconstructed beat clock, which a later v4 read cannot replace. So the v4 consent
comes first and its callback hands on to the ordinary Polar one.
"""

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.redis_client import get_redis_client
from app.models import UserConnection
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import OAuthState
from app.services.provider_settings_service import provider_settings_service
from app.services.providers.factory import ProviderFactory
from tests.factories import ApiKeyFactory, UserConnectionFactory, UserFactory
from tests.utils import api_key_headers

V4_AUTHORIZE = "https://auth.polar.com/oauth/authorize"
V3_AUTHORIZE = "https://flow.polar.com/oauth2/authorization"


def _state_of(auth_url: str) -> OAuthState:
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    raw = get_redis_client().get(f"oauth_state:{state}")
    assert raw is not None
    data = json.loads(raw)
    data.pop("code_verifier", None)
    return OAuthState.model_validate(data)


def _authorize(client: TestClient, **params: str) -> str:
    response = client.get("/api/v1/oauth/polar/authorize", params=params)
    assert response.status_code == 200
    return response.json()["authorization_url"]


def _v4_token_response() -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"access_token": "v4_access", "token_type": "Bearer", "expires_in": 3600}
    response.raise_for_status = MagicMock()
    return response


class TestAuthorizeStartsWithV4:
    def test_polar_starts_with_the_v4_grant_and_carries_the_account_details(
        self, client: TestClient, db: Session
    ) -> None:
        user = UserFactory()

        url = _authorize(
            client,
            user_id=str(user.id),
            redirect_uri="https://app.example/users/x/pair/success",
            account_type="validation",
            account_email="p01@example.com",
        )

        assert url.startswith(V4_AUTHORIZE)
        state = _state_of(url)
        assert state.provider == ProviderName.POLAR_V4.value
        assert state.then_provider == ProviderName.POLAR.value
        assert state.redirect_uri == "https://app.example/users/x/pair/success"
        assert state.account_type == "validation"
        assert state.account_email == "p01@example.com"

    def test_straight_to_polar_when_the_user_already_holds_a_v4_token(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider=ProviderName.POLAR_V4.value, provider_user_id=None)

        url = _authorize(client, user_id=str(user.id))

        assert url.startswith(V3_AUTHORIZE)
        assert _state_of(url).then_provider is None

    def test_a_revoked_v4_token_does_not_count(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider=ProviderName.POLAR_V4.value,
            provider_user_id=None,
            status=ConnectionStatus.REVOKED,
        )

        assert _authorize(client, user_id=str(user.id)).startswith(V4_AUTHORIZE)

    def test_straight_to_polar_when_the_deployment_has_switched_v4_off(self, client: TestClient, db: Session) -> None:
        user = UserFactory()

        with patch.object(settings, "polar_v4_enabled", False):
            assert _authorize(client, user_id=str(user.id)).startswith(V3_AUTHORIZE)

    def test_reconnecting_one_named_account_reauthorizes_only_that_grant(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        polar = UserConnectionFactory(user=user, provider=ProviderName.POLAR.value)

        url = _authorize(client, user_id=str(user.id), connection_id=str(polar.id))

        assert url.startswith(V3_AUTHORIZE)

    def test_connecting_v4_directly_is_unchanged(self, client: TestClient, db: Session) -> None:
        user = UserFactory()

        response = client.get("/api/v1/oauth/polar_v4/authorize", params={"user_id": str(user.id)})

        url = response.json()["authorization_url"]
        assert url.startswith(V4_AUTHORIZE)
        assert _state_of(url).then_provider is None


class TestV4CallbackHandsOnToPolar:
    @patch("app.api.routes.v1.oauth.sync_vendor_data.delay")
    @patch("httpx.post")
    def test_v4_is_saved_then_the_polar_consent_follows(
        self, mock_post: MagicMock, mock_sync: MagicMock, client: TestClient, db: Session
    ) -> None:
        user = UserFactory()
        mock_post.return_value = _v4_token_response()
        v4_url = _authorize(
            client,
            user_id=str(user.id),
            redirect_uri="https://app.example/done",
            account_type="validation",
            account_email="p01@example.com",
        )
        state = parse_qs(urlparse(v4_url).query)["state"][0]

        response = client.get(
            "/api/v1/oauth/polar_v4/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 303
        next_url = response.headers["location"]
        assert next_url.startswith(V3_AUTHORIZE)
        polar_state = _state_of(next_url)
        assert polar_state.provider == ProviderName.POLAR.value
        assert polar_state.then_provider is None
        assert polar_state.redirect_uri == "https://app.example/done"
        assert polar_state.account_type == "validation"
        assert polar_state.account_email == "p01@example.com"

        v4 = db.query(UserConnection).filter_by(user_id=user.id, provider=ProviderName.POLAR_V4.value).one()
        assert v4.status == ConnectionStatus.ACTIVE
        assert v4.account_email == "p01@example.com"
        # The v4 token syncs nothing on its own; the backfill comes with the Polar grant.
        mock_sync.assert_not_called()

    def test_declining_v4_still_leads_on_to_polar(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        v4_url = _authorize(client, user_id=str(user.id), redirect_uri="https://app.example/done")
        state = parse_qs(urlparse(v4_url).query)["state"][0]

        response = client.get(
            "/api/v1/oauth/polar_v4/callback",
            params={"error": "access_denied", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 303
        next_url = response.headers["location"]
        assert next_url.startswith(V3_AUTHORIZE)
        assert _state_of(next_url).redirect_uri == "https://app.example/done"
        # The declined state is spent, so it cannot be replayed.
        assert get_redis_client().get(f"oauth_state:{state}") is None
        assert not db.query(UserConnection).filter_by(user_id=user.id, provider=ProviderName.POLAR_V4.value).count()

    def test_an_error_on_an_unchained_flow_still_ends_on_the_error_page(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        oauth = ProviderFactory().get_provider(ProviderName.POLAR_V4.value).oauth
        assert oauth
        _, state = oauth.get_authorization_url(user.id)

        response = client.get(
            "/api/v1/oauth/polar_v4/callback",
            params={"error": "access_denied", "state": state},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/api/v1/oauth/error")

    def test_an_unknown_state_on_an_error_ends_on_the_error_page(self, client: TestClient, db: Session) -> None:
        response = client.get(
            "/api/v1/oauth/polar_v4/callback",
            params={"error": "access_denied", "state": uuid4().hex},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].startswith("/api/v1/oauth/error")


class TestPolarIsOneProvider:
    def test_v4_is_not_listed_as_a_provider_of_its_own(self, client: TestClient, db: Session) -> None:
        providers = {p["provider"]: p for p in client.get("/api/v1/oauth/providers").json()}

        assert ProviderName.POLAR_V4.value not in providers
        assert providers["polar"]["add_on_grants"] == [ProviderName.POLAR_V4.value]

    def test_polar_lists_no_add_on_when_v4_is_switched_off(self, client: TestClient, db: Session) -> None:
        with patch.object(settings, "polar_v4_enabled", False):
            providers = {p["provider"]: p for p in client.get("/api/v1/oauth/providers").json()}

        assert providers["polar"]["add_on_grants"] == []

    def test_v4_cannot_be_configured_as_a_provider(self, db: Session) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            provider_settings_service.bulk_update_providers(db, {ProviderName.POLAR_V4.value: False})

    def test_v4_folds_into_the_polar_connection(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        headers = api_key_headers(ApiKeyFactory().plain_key)
        UserConnectionFactory(user=user, provider=ProviderName.POLAR.value)
        UserConnectionFactory(user=user, provider=ProviderName.POLAR_V4.value, provider_user_id=None)

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=headers)

        assert response.status_code == 200
        rows = response.json()
        assert [r["provider"] for r in rows] == [ProviderName.POLAR.value]
        assert rows[0]["add_on_grants"] == [ProviderName.POLAR_V4.value]

    def test_disconnecting_polar_withdraws_the_v4_grant(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        headers = api_key_headers(ApiKeyFactory().plain_key)
        UserConnectionFactory(user=user, provider=ProviderName.POLAR.value)
        v4 = UserConnectionFactory(user=user, provider=ProviderName.POLAR_V4.value, provider_user_id=None)

        with patch("app.services.providers.polar.oauth.PolarOAuth.deregister_user"):
            response = client.delete(f"/api/v1/users/{user.id}/connections/polar", headers=headers)

        assert response.status_code == 204
        db.refresh(v4)
        assert v4.status == ConnectionStatus.REVOKED
