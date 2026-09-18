import contextlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel

from app.database import DbSession
from app.models import ProviderSetting, UserConnection
from app.repositories.provider_settings_repository import ProviderSettingsRepository
from app.schemas.auth import ConnectionStatus, LiveSyncMode, SDKAuthContext
from app.schemas.enums import ProviderName
from app.schemas.model_crud.user_management import (
    UserConnectionAccountUpdate,
    UserConnectionWithCapabilities,
)
from app.services import ApiKeyDep, user_connection_service
from app.services.providers.base_strategy import BaseProviderStrategy
from app.services.providers.factory import ProviderFactory
from app.utils.auth import CombinedAuthDep

router = APIRouter()
factory = ProviderFactory()
provider_settings_repo = ProviderSettingsRepository()


def _with_capabilities(
    conn: object,
    settings_map: dict[str, ProviderSetting],
    linked_user_ids: list | None = None,
    account_index: int = 1,
    account_count: int = 1,
) -> UserConnectionWithCapabilities:
    enriched = UserConnectionWithCapabilities.model_validate(conn)
    enriched.account_index = account_index
    enriched.account_count = account_count
    with contextlib.suppress(ValueError):
        strategy = factory.get_provider(enriched.provider)
        caps = strategy.capabilities
        enriched.icon_url = strategy.icon_url
        enriched.max_historical_days = caps.max_historical_days
        enriched.rest_pull = caps.rest_pull
        enriched.webhook_stream = caps.webhook_stream
        enriched.webhook_ping = caps.webhook_ping
        enriched.webhook_callback = caps.webhook_callback
        setting = settings_map.get(enriched.provider)
        mode = (
            setting.live_sync_mode
            if (setting and setting.live_sync_mode is not None)
            else strategy.default_live_sync_mode
        )
        # ORM yields a plain str and attribute assignment skips validation; coerce to the enum
        enriched.live_sync_mode = LiveSyncMode(mode) if mode is not None else None
    if linked_user_ids:
        enriched.linked_user_ids = linked_user_ids
    return enriched


@router.get("/users/{user_id}/connections", response_model=list[UserConnectionWithCapabilities])
def get_connections_endpoint(
    user_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
):
    """Get all connections for a user, enriched with provider capability metadata.

    A user may hold several accounts with the same provider, so this list can
    contain more than one entry per provider. ``account_index`` / ``account_count``
    position each one within its provider (oldest first) and ``display_label``
    names it, so a client can render three Garmins without guessing which is which.
    """
    settings_map = provider_settings_repo.get_all(db)
    connections = user_connection_service.get_connections_by_user(db, user_id)
    provider_pairs = [
        (c.provider, c.provider_user_id)
        for c in connections
        if c.provider_user_id and c.status == ConnectionStatus.ACTIVE
    ]
    linked_map = user_connection_service.get_linked_user_ids(db, user_id, provider_pairs)
    positions = _account_positions(connections)
    return [
        _with_capabilities(
            conn,
            settings_map,
            linked_map.get((conn.provider, conn.provider_user_id)) if conn.provider_user_id else None,
            *positions[conn.id],
        )
        for conn in connections
    ]


def _account_positions(connections: list) -> dict[UUID, tuple[int, int]]:
    """Map each connection to its (index, count) within its provider.

    Ordered by creation so the numbering is stable: an account that was second
    when it was added stays second after a sibling is disconnected, and a label
    printed on a device in a lab does not go stale.
    """
    by_provider: dict[str, list] = {}
    for conn in sorted(connections, key=lambda c: (c.created_at, str(c.id))):
        by_provider.setdefault(conn.provider, []).append(conn)
    positions: dict[UUID, tuple[int, int]] = {}
    for group in by_provider.values():
        for index, conn in enumerate(group, start=1):
            positions[conn.id] = (index, len(group))
    return positions


# ---------------------------------------------------------------------------
# Per-account endpoints.
#
# Declared before the /{provider} routes below because FastAPI matches in
# declaration order, and "accounts" would otherwise be offered to the
# ProviderName-typed {provider} parameter first.
#
# These are the addressable form of a connection. The provider-scoped routes
# that follow still work and still mean "this provider, all of it", which is
# the right reading of "disconnect me from Whoop" - but they cannot name one of
# several accounts, and anything that has to (relabelling, purging one arm of a
# study, reconnecting a single login) goes through these.
# ---------------------------------------------------------------------------


def _account_or_404(db: DbSession, user_id: UUID, connection_id: UUID) -> UserConnection:
    connection = user_connection_service.get_account(db, user_id, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connected account not found for this user")
    return connection


def _enriched_account(db: DbSession, connection: UserConnection) -> UserConnectionWithCapabilities:
    settings_map = provider_settings_repo.get_all(db)
    siblings = user_connection_service.get_accounts_for_provider(db, connection.user_id, connection.provider)
    index = next((i for i, c in enumerate(siblings, start=1) if c.id == connection.id), 1)
    return _with_capabilities(connection, settings_map, None, index, len(siblings))


@router.get("/users/{user_id}/connections/accounts/{connection_id}")
def get_connection_account_endpoint(
    user_id: UUID,
    connection_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> UserConnectionWithCapabilities:
    """One connected provider account, by connection id."""
    return _enriched_account(db, _account_or_404(db, user_id, connection_id))


@router.patch("/users/{user_id}/connections/accounts/{connection_id}")
def update_connection_account_endpoint(
    user_id: UUID,
    connection_id: UUID,
    body: UserConnectionAccountUpdate,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> UserConnectionWithCapabilities:
    """Rename a connected account, or correct its e-mail or the device behind it.

    Fields absent from the body are left alone; sending an explicit ``null``
    clears one. Setting ``device_label`` also stamps it onto this account's
    already-ingested, device-less data sources, so history and future samples
    agree on what was worn.
    """
    _account_or_404(db, user_id, connection_id)
    updated = user_connection_service.update_account(db, user_id, connection_id, body, set(body.model_fields_set))
    if updated is None:
        raise HTTPException(status_code=404, detail="Connected account not found for this user")
    return _enriched_account(db, updated)


@router.delete(
    "/users/{user_id}/connections/accounts/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def disconnect_connection_account_endpoint(
    user_id: UUID,
    connection_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> Response:
    """Disconnect one account, leaving the user's other accounts with the same provider alone."""
    connection = _account_or_404(db, user_id, connection_id)
    strategy = ProviderFactory().get_provider(connection.provider)
    user_connection_service.disconnect(
        db,
        user_id,
        connection.provider,
        oauth=strategy.oauth,
        connection_id=connection_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/users/{user_id}/connections/accounts/{connection_id}/data",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_connection_account_data_endpoint(
    user_id: UUID,
    connection_id: UUID,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> Response:
    """Revoke one account and delete only the data that came through it.

    The user's other accounts with the same provider keep their data. Health
    scores computed without a data source are left alone, because they cannot be
    attributed to one of several accounts - use the provider-level purge to
    clear those.
    """
    connection = _account_or_404(db, user_id, connection_id)
    strategy = ProviderFactory().get_provider(connection.provider)
    user_connection_service.purge_provider_data(
        db,
        user_id,
        connection.provider,
        oauth=strategy.oauth,
        connection_id=connection_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _assert_sdk_token_may_disconnect(
    db: DbSession,
    auth: SDKAuthContext,
    user_id: UUID,
    strategy: BaseProviderStrategy,
) -> None:
    """Confine an SDK-token caller to its own user's SDK-fed connections.

    The token carries no provider claim, so ``client_sdk`` gates which providers are
    reachable at all. The token check behind it is defence in depth: no SDK provider
    holds OAuth tokens today, and an SDK sign-out must never force a re-authorization.
    """
    if auth.user_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token does not match user_id")

    if not strategy.capabilities.client_sdk:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SDK tokens cannot disconnect this provider")

    connection = user_connection_service.get_connection(db, user_id, strategy.name)
    if connection and (connection.access_token or connection.refresh_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "SDK tokens cannot disconnect an OAuth connection")


@router.delete("/users/{user_id}/connections/{provider}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_provider_endpoint(
    user_id: UUID,
    provider: ProviderName,
    db: DbSession,
    auth: CombinedAuthDep,
) -> Response:
    """Disconnect a user from a provider, revoking the connection(s) and clearing tokens.

    Disconnects *every* account the user holds with this provider. To disconnect
    one of several, use DELETE /users/{user_id}/connections/accounts/{connection_id}.

    Also takes an SDK user token, so the mobile SDK can report a sign-out its local-only
    ``signOut()`` would otherwise hide. That path skips provider deregistration: leaving an
    app is no reason to unregister the user from the provider's API.
    """
    strategy = ProviderFactory().get_provider(provider.value)

    if auth.auth_type == "sdk_token":
        _assert_sdk_token_may_disconnect(db, auth, user_id, strategy)
        user_connection_service.disconnect(db, user_id, provider.value, oauth=None, reason="sdk_sign_out")
    else:
        user_connection_service.disconnect(db, user_id, provider.value, oauth=strategy.oauth)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


class DeviceLabelUpdate(BaseModel):
    """Device identifier to attach to a connection (null to clear)."""

    device_label: str | None = None


class DeviceLabelResponse(BaseModel):
    provider: str
    device_label: str | None
    # Which account was actually labelled - the caller may not have said.
    connection_id: UUID


@router.put("/users/{user_id}/connections/{provider}/device-label")
def set_connection_device_label_endpoint(
    user_id: UUID,
    provider: ProviderName,
    body: DeviceLabelUpdate,
    db: DbSession,
    _api_key: ApiKeyDep,
    connection_id: Annotated[UUID | None, Query(description="Which account, when the user has several")] = None,
) -> DeviceLabelResponse:
    """Manually set the device model behind a connection (e.g. "Whoop 5.0").

    Fills data_source.device_model for providers whose API reports no device.
    Applies to existing device-less data sources and future ingested data.

    Pass ``connection_id`` when the user holds several accounts with this
    provider; without it the oldest is used, which is unambiguous only for the
    single-account case. The PATCH .../connections/accounts/{connection_id}
    endpoint does the same thing and names the account in the path.
    """
    connection = user_connection_service.set_device_label(db, user_id, provider, body.device_label, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"No {provider.value} connection found for user")
    return DeviceLabelResponse(
        provider=provider.value,
        device_label=connection.device_label,
        connection_id=connection.id,
    )


@router.delete("/users/{user_id}/connections/{provider}/data", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider_data_endpoint(
    user_id: UUID,
    provider: ProviderName,
    db: DbSession,
    _api_key: ApiKeyDep,
) -> Response:
    """Delete all of a user's data for a provider and revoke its connection(s).

    Covers every account the user holds with this provider. To purge one of
    several, use DELETE /users/{user_id}/connections/accounts/{connection_id}/data.
    """
    strategy = ProviderFactory().get_provider(provider.value)
    user_connection_service.purge_provider_data(db, user_id, provider.value, oauth=strategy.oauth)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
