from datetime import datetime, timezone
from logging import Logger, getLogger
from uuid import UUID

from app.database import DbSession
from app.models import UserConnection
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import ProviderName, SdkConnectionOutcome
from app.schemas.model_crud.user_management import (
    UserConnectionAccountUpdate,
    UserConnectionCreate,
    UserConnectionUpdate,
)
from app.schemas.responses.upload import ConnectionsCoverage, ProviderConnectionCount
from app.services.outgoing_webhooks.events import on_connection_created, on_connection_revoked
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.services import AppService
from app.utils.exceptions import ResourceAlreadyExistsError, ResourceNotFoundError, handle_exceptions
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured


class UserConnectionService(
    AppService[UserConnectionRepository, UserConnection, UserConnectionCreate, UserConnectionUpdate],
):
    def __init__(self, log: Logger, **kwargs):
        super().__init__(
            crud_model=UserConnectionRepository,
            model=UserConnection,
            log=log,
            **kwargs,
        )
        self.data_source_crud = DataSourceRepository()

    @handle_exceptions
    def set_device_label(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_label: str | None,
        connection_id: UUID | None = None,
    ) -> UserConnection | None:
        """Set the device label on one connected account and relabel that
        account's existing device-less data sources so already-ingested data
        also carries it (future data is filled by ensure_data_source).

        Without ``connection_id`` this addresses the user's connection for the
        provider the way it always did - which is still unambiguous for the many
        users who hold one account, and picks the oldest with a logged warning
        for those who hold several.

        Returns None when the account does not exist.
        """
        connection = self._resolve(db_session, user_id, provider.value, connection_id)
        if connection is None:
            return None
        connection.device_label = device_label
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        self.data_source_crud.set_connection_device_label(db_session, user_id, provider, device_label, connection.id)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def _resolve(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        connection_id: UUID | None,
    ) -> UserConnection | None:
        """One account, by id when given and by (user, provider) otherwise.

        An id belonging to another user or another provider resolves to None
        rather than to some other account: addressing the wrong wearable is a
        worse outcome than a 404.
        """
        if connection_id is None:
            return self.crud.get_by_user_and_provider(db_session, user_id, provider)
        connection = self.crud.get_by_id_for_user(db_session, user_id, connection_id)
        if connection is None or connection.provider != provider:
            return None
        return connection

    @handle_exceptions
    def get_account(
        self,
        db_session: DbSession,
        user_id: UUID,
        connection_id: UUID,
    ) -> UserConnection | None:
        """One of the user's connected accounts by id."""
        return self.crud.get_by_id_for_user(db_session, user_id, connection_id)

    @handle_exceptions
    def get_accounts_for_provider(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        *,
        active_only: bool = False,
    ) -> list[UserConnection]:
        """Every account this user holds with one provider, oldest first."""
        return self.crud.get_all_by_user_and_provider(db_session, user_id, provider, active_only=active_only)

    @handle_exceptions
    def update_account(
        self,
        db_session: DbSession,
        user_id: UUID,
        connection_id: UUID,
        payload: UserConnectionAccountUpdate,
        fields_set: set[str],
    ) -> UserConnection | None:
        """Rename an account, or correct the e-mail or device recorded for it.

        ``fields_set`` is the request's own set of supplied fields, so an
        explicit ``null`` clears a value while an omitted field is left alone.
        The two cannot be told apart from the parsed payload, and conflating
        them would let a rename silently drop the e-mail that says which
        account the data came from.
        """
        connection = self.crud.get_by_id_for_user(db_session, user_id, connection_id)
        if connection is None:
            return None

        if "account_email" in fields_set and payload.account_email is not None:
            clash = self.crud.get_by_account_email(db_session, user_id, connection.provider, str(payload.account_email))
            if clash is not None and clash.id != connection.id:
                raise ResourceAlreadyExistsError(
                    f"Another {connection.provider} account for this user already uses that e-mail",
                )

        updated = self.crud.update_account_metadata(
            db_session,
            connection,
            account_label=payload.account_label,
            account_email=str(payload.account_email) if payload.account_email is not None else None,
            device_label=payload.device_label,
            clear_account_label="account_label" in fields_set and payload.account_label is None,
            clear_account_email="account_email" in fields_set and payload.account_email is None,
            clear_device_label="device_label" in fields_set and payload.device_label is None,
        )

        # A device label is also stamped onto already-ingested, device-less data
        # so history and future samples agree on what was worn.
        if "device_label" in fields_set and payload.device_label:
            self.data_source_crud.set_connection_device_label(
                db_session,
                user_id,
                ProviderName(connection.provider),
                payload.device_label,
                connection.id,
            )
            db_session.commit()

        return updated

    def get_active_count_in_range(self, db_session: DbSession, start_date: datetime, end_date: datetime) -> int:
        """Get count of active connections created within a date range."""
        return self.crud.get_active_count_in_range(db_session, start_date, end_date)

    def get_connections_coverage(self, db_session: DbSession) -> ConnectionsCoverage:
        """Aggregate coverage stats: users with active conn, multi-conn, top providers."""
        return ConnectionsCoverage(
            users_with_active=self.crud.get_users_with_active_conn_count(db_session),
            users_with_multi_active=self.crud.get_users_with_multi_active_conn_count(db_session),
            top_providers=[
                ProviderConnectionCount(provider=p, count=c)
                for p, c in self.crud.get_top_providers_by_active_conn(db_session, limit=6)
            ],
        )

    @handle_exceptions
    def get_connections_by_user(self, db_session: DbSession, user_id: UUID) -> list[UserConnection]:
        """Get all connections for a user."""
        return self.crud.get_by_user_id(db_session, user_id)

    @handle_exceptions
    def get_connection(self, db_session: DbSession, user_id: UUID, provider: str) -> UserConnection | None:
        """Get a user's connection for one provider, or None if there is none."""
        return self.crud.get_by_user_and_provider(db_session, user_id, provider)

    def get_linked_user_ids(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], list[UUID]]:
        """Return other active OW users sharing the same external account, grouped by (provider, provider_user_id)."""
        return self.crud.get_linked_user_ids(db_session, user_id, provider_pairs)

    def ensure_sdk_connection(self, db_session: DbSession, user_id: UUID, provider: str) -> UserConnection:
        """Ensure an SDK connection exists, emitting ``connection.created`` on state change.

        SDK providers have no OAuth callback, so this is where their connection is born --
        on the first upload. ``connected_at`` is taken from the row rather than ``now()``
        because it feeds the webhook idempotency key: this runs on every batch and the
        Celery task retries, so a row-derived key is what collapses the duplicates.
        """
        connection, outcome = self.crud.ensure_sdk_connection(db_session, user_id, provider)

        if outcome == SdkConnectionOutcome.EXISTING:
            return connection

        connected_at = connection.created_at if outcome == SdkConnectionOutcome.CREATED else connection.updated_at

        log_structured(
            self.logger,
            "info",
            f"SDK connection {outcome}",
            action="sdk_connection_state_change",
            outcome=str(outcome),
            provider=provider,
            user_id=str(user_id),
            connection_id=str(connection.id),
            connected_at=connected_at.isoformat(),
        )

        on_connection_created(
            user_id=user_id,
            provider=provider,
            connection_id=connection.id,
            connected_at=connected_at.isoformat(),
        )
        return connection

    @handle_exceptions
    def disconnect(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        oauth: BaseOAuthTemplate | None = None,
        reason: str = "user_disconnected",
        connection_id: UUID | None = None,
    ) -> None:
        """Disconnect a user from a provider. Raises 404 if no connection exists.

        With ``connection_id`` only that account is disconnected; without it,
        every account the user holds with the provider is, which is what
        "disconnect me from Garmin" has always meant and still means.

        Each account is deregistered and revoked on its own and gets its own
        ``connection.revoked`` webhook: consumers key off the connection id, and
        one event for three revoked accounts would leave two of them looking
        connected downstream.

        If oauth is provided, calls the provider's deregistration API before
        clearing tokens. Deregistration failures are logged but do not block.
        """
        targets = self._disconnect_targets(db_session, user_id, provider, connection_id)
        if not targets:
            raise ResourceNotFoundError("connection", user_id)

        revoked_any = False
        for connection in targets:
            if oauth:
                self._deregister_from_provider(db_session, user_id, provider, oauth, connection)

            if not self.crud.disconnect(db_session, user_id, provider, connection.id):
                # Already revoked - nothing changed, so nothing to announce.
                continue

            revoked_any = True
            db_session.refresh(connection)
            log_structured(
                self.logger,
                "info",
                "Connection revoked",
                action="connection_revoked",
                reason=reason,
                provider=provider,
                user_id=str(user_id),
                connection_id=str(connection.id),
            )
            on_connection_revoked(
                user_id=user_id,
                provider=provider,
                connection_id=connection.id,
                reason=reason,
                revoked_at=connection.updated_at.isoformat(),
            )

        if not revoked_any:
            # Every target was already revoked. Idempotent, as before: the caller
            # asked for a state that already holds.
            return

    def _disconnect_targets(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        connection_id: UUID | None,
    ) -> list[UserConnection]:
        """The accounts a disconnect call applies to."""
        if connection_id is None:
            return self.crud.get_all_by_user_and_provider(db_session, user_id, provider)
        connection = self.crud.get_by_id_for_user(db_session, user_id, connection_id)
        if connection is None or connection.provider != provider:
            return []
        return [connection]

    @handle_exceptions
    def purge_provider_data(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        oauth: BaseOAuthTemplate | None = None,
        connection_id: UUID | None = None,
    ) -> int:
        """Revoke the connection(s) and delete the user's data for the provider.

        Runs the standard disconnect (best-effort deregistration, revoke, webhook), then
        deletes the user's data_source rows for the provider. ON DELETE CASCADE removes all
        dependent event records, series, details and health scores. Returns the number of
        data_source rows deleted. Safe to call on an already-revoked connection.

        With ``connection_id`` only that account's data is deleted; the other
        accounts the user holds with the same provider are untouched, which is
        what makes it safe to drop one arm of a validation study without losing
        the comparator worn beside it.
        """
        self.disconnect(db_session, user_id, provider, oauth=oauth, connection_id=connection_id)
        if connection_id is None:
            deleted = self.data_source_crud.delete_user_provider_data(db_session, user_id, ProviderName(provider))
        else:
            deleted = self.data_source_crud.delete_connection_data(db_session, user_id, connection_id)
        self.logger.info(
            "Purged %s data sources for user %s from provider %s (connection=%s)",
            deleted,
            user_id,
            provider,
            connection_id or "all",
        )
        return deleted

    @handle_exceptions
    def stamp_last_synced_at(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        connection_id: UUID | None = None,
    ) -> None:
        """Stamp last_synced_at=now on the user's connection for the given provider.

        Used after OAuth completion so the first periodic sync uses the connection
        timestamp as its live-sync cursor and won't attempt to pull all historical data.
        No-op if the connection does not exist.
        """
        connection = self._resolve(db_session, user_id, provider, connection_id)
        if connection:
            self.crud.update_last_synced_at(db_session, connection)

    def _deregister_from_provider(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        oauth: BaseOAuthTemplate,
        connection: UserConnection | None = None,
    ) -> None:
        """Best-effort call to provider's deregistration API, for one account."""
        if connection is None:
            connection = self.crud.get_by_user_and_provider(db_session, user_id, provider)
        if not connection or not connection.access_token:
            return

        try:
            oauth.deregister_user(
                connection.access_token,
                provider_user_id=connection.provider_user_id,
            )
            log_structured(
                self.logger,
                "info",
                "Deregistered user from provider API",
                provider=provider,
                task="deregister_user",
                user_id=str(user_id),
            )
        except Exception as e:
            log_structured(
                self.logger,
                "error",
                f"Failed to deregister user from provider API: {e}",
                provider=provider,
                task="deregister_user",
                user_id=str(user_id),
            )
            log_and_capture_error(
                e,
                self.logger,
                f"Failed to deregister user {user_id} from {provider} API: {e}",
                extra={"user_id": str(user_id), "provider": provider},
            )


user_connection_service = UserConnectionService(log=getLogger(__name__))
