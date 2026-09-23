from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from app.config import settings
from app.constants.provider_urls import from_url_slug
from app.database import DbSession
from app.models import UserConnection
from app.schemas.enums import AccountType, ProviderName
from app.schemas.model_crud.credentials import AuthorizationURLResponse, OAuthState
from app.schemas.model_crud.data_priority import (
    BulkProviderSettingsUpdate,
    ProviderSettingRead,
    ProviderSettingUpdate,
)
from app.services import DeveloperDep, user_connection_service
from app.services.provider_settings_service import ProviderSettingsService
from app.services.providers.base_strategy import BaseProviderStrategy
from app.services.providers.factory import ProviderFactory

router = APIRouter()
factory = ProviderFactory()
settings_service = ProviderSettingsService()


def resolve_provider(slug: str) -> ProviderName:
    # 400 rather than 404 keeps the response the enum-typed parameter used to give.
    try:
        return ProviderName(from_url_slug(slug))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown provider: '{slug}'")


def get_oauth_strategy(provider: ProviderName) -> BaseProviderStrategy:
    """Helper to get provider strategy and ensure it supports OAuth."""
    strategy = factory.get_provider(provider.value)

    if not strategy.oauth:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider '{provider.value}' does not support OAuth",
        )
    return strategy


@router.get(
    "/{provider}/authorize",
    summary="Get Provider Authorization URL",
    response_model=AuthorizationURLResponse,
    tags=["External: Providers"],
)
def authorize_provider(
    provider: str,
    user_id: Annotated[UUID, Query(description="User ID to connect")],
    redirect_uri: Annotated[str | None, Query(description="Optional redirect URI after authorization")] = None,
    connection_id: Annotated[
        UUID | None,
        Query(description="Re-authorize this existing account instead of adding another"),
    ] = None,
    new_account: Annotated[
        bool,
        Query(description="Add another account with this provider beside the ones already linked"),
    ] = False,
    account_type: Annotated[
        AccountType | None,
        Query(description="What this account is for: personal, validation, reliability, monitoring, testing, other"),
    ] = None,
    account_label: Annotated[
        str | None,
        Query(max_length=100, description='Operator-facing name for this account, e.g. "P01 arm A"'),
    ] = None,
    account_email: Annotated[
        str | None,
        Query(description="Login e-mail of the provider account, when the provider does not report it"),
    ] = None,
):
    """
    Initiate OAuth flow for a provider.

    A user may hold several accounts with the same provider - two Whoops worn
    simultaneously for a reliability study, say. By default this endpoint
    behaves as it always has: with no existing account it creates one, and with
    exactly one it re-authorizes that one. Pass ``new_account=true`` to add
    another, or ``connection_id`` to re-authorize a specific one.

    ``account_type``, ``account_label`` and ``account_email`` are carried through
    the OAuth flow and recorded on whichever account the callback resolves.
    ``account_type`` is what a study filters on - the validation arm separated
    from the participant's own everyday wear. The e-mail is what ties a data set
    back to the login it came from, so pass it for providers whose API does not
    expose it (Garmin, Polar, Suunto, Strava, Withings).

    Returns authorization URL where user should be redirected to log in.
    """
    strategy = get_oauth_strategy(resolve_provider(provider))

    if connection_id is not None and new_account:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "connection_id re-authorizes an existing account and new_account adds one; pass at most one",
        )

    assert strategy.oauth
    auth_url, state = strategy.oauth.get_authorization_url(
        user_id,
        redirect_uri,
        connection_id=connection_id,
        new_account=new_account,
        account_type=account_type.value if account_type else None,
        account_label=account_label,
        account_email=account_email,
    )
    return AuthorizationURLResponse(authorization_url=auth_url, state=state)


def _just_connected(db: DbSession, oauth_state: OAuthState, provider_name: ProviderName) -> UserConnection | None:
    """The account the callback just authorized.

    ``connection_id`` in the state is authoritative when it is there - it is the
    account the flow was started for. Otherwise the newest account for this
    user and provider is the one the callback created or refreshed, since the
    callback is the only thing that writes them and it has just run.
    """
    if oauth_state.connection_id is not None:
        return user_connection_service.get_account(db, oauth_state.user_id, oauth_state.connection_id)
    accounts = user_connection_service.get_accounts_for_provider(db, oauth_state.user_id, provider_name.value)
    return accounts[-1] if accounts else None


@router.head("/{provider}/callback", tags=["System: OAuth"])
def probe_oauth_callback(provider: str) -> None:
    """Answer the reachability probe Withings sends when the callback URL is registered."""
    get_oauth_strategy(resolve_provider(provider))


@router.get("/{provider}/callback", tags=["System: OAuth"])
def oauth_callback(
    provider: str,
    db: DbSession,
    code: Annotated[str | None, Query(description="Authorization code from provider")] = None,
    state: Annotated[str | None, Query(description="State parameter for CSRF protection")] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
):
    """
    OAuth callback endpoint.

    Provider redirects here after user authorizes. Exchanges code for tokens.
    """
    if error:
        return RedirectResponse(
            url=f"/api/v1/oauth/error?message={error}:+{error_description or 'Unknown+error'}",
            status_code=303,
        )

    if not code or not state:
        return RedirectResponse(
            url="/api/v1/oauth/error?message=Missing+OAuth+parameters",
            status_code=303,
        )

    provider_name = resolve_provider(provider)
    strategy = get_oauth_strategy(provider_name)

    assert strategy.oauth
    oauth_state = strategy.oauth.handle_callback(db, code, state)

    # Which account the callback landed on. With several accounts on one
    # provider, "the user's connection" is no longer a single row, so the
    # follow-up work below has to name the one that was just authorized.
    connected = _just_connected(db, oauth_state, provider_name)
    connection_id = connected.id if connected else None

    # Stamp last_synced_at=now so the first periodic sync uses the connection
    # timestamp as its live-sync cursor and won't attempt to pull all history.
    user_connection_service.stamp_last_synced_at(
        db, oauth_state.user_id, provider_name.value, connection_id=connection_id
    )

    # Grace-period flag: automatically kick off a historical sync so integrators
    # who haven't yet adopted the explicit /sync/historical call still get backfill.
    # Controlled by HISTORICAL_SYNC_ON_CONNECT (default: true).
    if settings.historical_sync_on_connect:
        caps = strategy.capabilities
        if caps.webhook_callback:
            # this code is going to be removed later, so leave inner imports heres
            from app.integrations.celery.tasks import start_garmin_full_backfill

            start_garmin_full_backfill.delay(
                str(oauth_state.user_id),
                connection_id=str(connection_id) if connection_id else None,
            )
        elif caps.rest_pull:
            from app.integrations.celery.tasks import sync_vendor_data

            now = datetime.now(timezone.utc)
            start_date = (now - timedelta(days=90)).isoformat()
            sync_vendor_data.delay(
                user_id=str(oauth_state.user_id),
                start_date=start_date,
                end_date=now.isoformat(),
                providers=[provider_name.value],
                is_historical=True,
                # Only the account that was just connected needs a backfill; the
                # others already have theirs and re-pulling 90 days for each of
                # them on every new connection would be wasteful and rate-limited.
                connection_ids=[str(connection_id)] if connection_id else None,
            )

    # If a specific redirect_uri was requested (e.g. by frontend), redirect there
    if oauth_state.redirect_uri:
        return RedirectResponse(url=oauth_state.redirect_uri, status_code=303)

    # Otherwise, redirect to internal success page
    return RedirectResponse(
        url=f"/api/v1/oauth/success?provider={provider_name.value}&user_id={oauth_state.user_id}",
        status_code=303,
    )


@router.get("/success", tags=["System: OAuth"])
def oauth_success(
    provider: Annotated[str, Query()],
    user_id: Annotated[str, Query()],
) -> dict:
    """Simple success page after OAuth completion."""
    return {
        "success": True,
        "message": f"Successfully connected to {provider}",
        "user_id": user_id,
        "provider": provider,
    }


@router.get("/error", tags=["System: OAuth"])
def oauth_error(
    message: Annotated[str, Query()] = "OAuth authentication failed",
) -> dict:
    """OAuth error page."""
    return {
        "success": False,
        "message": message,
    }


@router.get("/providers", response_model=list[ProviderSettingRead], tags=["External: Providers"])
def get_providers(
    db: DbSession,
    enabled_only: Annotated[bool, Query(description="Return only enabled providers")] = False,
    cloud_only: Annotated[bool, Query(description="Return only cloud (OAuth) providers")] = False,
):
    """
    Get providers with their configuration and metadata.

    Query params:
    - enabled_only: Filter to only enabled providers (default: False, returns all)
    - cloud_only: Filter to only providers with cloud OAuth API (default: False)

    Returns full provider details including name, icon_url, has_cloud_api, is_enabled.
    """
    all_providers = settings_service.get_all_providers(db)

    return [p for p in all_providers if (not enabled_only or p.is_enabled) and (not cloud_only or p.has_cloud_api)]


@router.put("/providers/{provider}", response_model=ProviderSettingRead, tags=["Internal: Providers"])
def update_provider_setting(
    provider: str,
    update: ProviderSettingUpdate,
    db: DbSession,
    _developer: DeveloperDep,
):
    """Update is_enabled and/or live_sync_mode for a single provider."""
    try:
        return settings_service.update_provider_setting(db, provider, update)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.put("/providers", response_model=list[ProviderSettingRead], tags=["Internal: Providers"])
def bulk_update_providers(
    updates: BulkProviderSettingsUpdate,
    db: DbSession,
    _developer: DeveloperDep,
):
    """
    Bulk update provider settings.

    Accepts a map of provider_id -> is_enabled and updates all providers at once.
    This is the primary endpoint for the admin UI to save checkbox states.
    """
    return settings_service.bulk_update_providers(db, updates.providers)
