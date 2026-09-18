from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, func, select, tuple_, update
from sqlalchemy.orm import Query
from sqlalchemy.orm.exc import MultipleResultsFound

from app.database import DbSession
from app.models import UserConnection
from app.repositories.repositories import CrudRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import SdkConnectionOutcome
from app.schemas.model_crud.user_management import (
    UserConnectionCreate,
    UserConnectionUpdate,
)
from app.utils.connection_context import get_active_connection_id

logger = getLogger(__name__)


class UserConnectionRepository(CrudRepository[UserConnection, UserConnectionCreate, UserConnectionUpdate]):
    """Repository for managing OAuth user connections to fitness providers."""

    def __init__(self, model: type[UserConnection] = UserConnection):
        super().__init__(model)

    def get_active_count(self, db_session: DbSession) -> int:
        """Get total count of active connections."""
        return (
            db_session.query(func.count(self.model.id)).filter(self.model.status == ConnectionStatus.ACTIVE).scalar()
            or 0
        )

    def get_active_count_in_range(self, db_session: DbSession, start_date: datetime, end_date: datetime) -> int:
        """Get count of active connections created within a date range."""
        return (
            db_session.query(func.count(self.model.id))
            .filter(
                and_(
                    self.model.status == ConnectionStatus.ACTIVE,
                    self.model.created_at >= start_date,
                    self.model.created_at < end_date,
                ),
            )
            .scalar()
            or 0
        )

    def get_users_with_active_conn_count(self, db_session: DbSession) -> int:
        """Count of distinct users with at least one active connection."""
        return (
            db_session.query(func.count(func.distinct(self.model.user_id)))
            .filter(self.model.status == ConnectionStatus.ACTIVE)
            .scalar()
            or 0
        )

    def get_users_with_multi_active_conn_count(self, db_session: DbSession) -> int:
        """Count of distinct users with more than one active connection."""
        subq = (
            select(self.model.user_id)
            .where(self.model.status == ConnectionStatus.ACTIVE)
            .group_by(self.model.user_id)
            .having(func.count(self.model.id) > 1)
            .subquery()
        )
        return db_session.query(func.count()).select_from(subq).scalar() or 0

    def get_top_providers_by_active_conn(self, db_session: DbSession, limit: int = 3) -> list[tuple[str, int]]:
        """Top providers by active connection count, returns (provider, count) pairs."""
        rows = (
            db_session.query(self.model.provider, func.count(self.model.id).label("cnt"))
            .filter(self.model.status == ConnectionStatus.ACTIVE)
            .group_by(self.model.provider)
            .order_by(func.count(self.model.id).desc())
            .limit(limit)
            .all()
        )
        return [(row.provider, row.cnt) for row in rows]

    def _user_provider_query(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        *,
        active_only: bool = False,
    ) -> Query[UserConnection]:
        """Base query for a user's accounts with one provider, oldest first.

        The ordering is what makes the single-connection accessors deterministic
        once a user holds several accounts: "the first one" has to mean the same
        row across query plans and restarts, or an unscoped caller would drift
        between accounts from run to run.
        """
        conditions = [self.model.user_id == user_id, self.model.provider == provider]
        if active_only:
            conditions.append(self.model.status == ConnectionStatus.ACTIVE)
        return (
            db_session.query(self.model)
            .filter(and_(*conditions))
            .order_by(self.model.created_at.asc(), self.model.id.asc())
        )

    def get_by_id_for_user(
        self,
        db_session: DbSession,
        user_id: UUID,
        connection_id: UUID,
    ) -> UserConnection | None:
        """Get one connection by id, scoped to its owner.

        The user_id is part of the lookup rather than checked afterwards so a
        connection id from one user can never address another user's account.
        """
        return (
            db_session.query(self.model)
            .filter(and_(self.model.id == connection_id, self.model.user_id == user_id))
            .one_or_none()
        )

    def get_all_by_user_and_provider(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        *,
        active_only: bool = False,
    ) -> list[UserConnection]:
        """Every account this user holds with one provider, oldest first."""
        return self._user_provider_query(db_session, user_id, provider, active_only=active_only).all()

    def _scoped_connection(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
    ) -> UserConnection | None:
        """The connection the current unit of work declared, if it matches the lookup.

        Returns None when no scope is set, or when the scope names a connection
        belonging to a different user or provider - a mismatch means the caller
        is asking about something other than what it scoped, and answering with
        the scoped row would be the wrong account rather than a fallback.
        """
        connection_id = get_active_connection_id()
        if connection_id is None:
            return None
        connection = db_session.get(self.model, connection_id)
        if connection is None or connection.user_id != user_id or connection.provider != provider:
            return None
        return connection

    def get_by_user_and_provider(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
    ) -> UserConnection | None:
        """Get a connection for a user and provider.

        Inside an :func:`~app.utils.connection_context.active_connection` scope
        this returns that account. Outside one it returns the oldest connection
        and warns when the user holds more than one, because the caller has then
        asked a question that no longer has a single answer and the row it gets
        back may not be the account it meant.
        """
        scoped = self._scoped_connection(db_session, user_id, provider)
        if scoped is not None:
            return scoped

        connections = self._user_provider_query(db_session, user_id, provider).all()
        if not connections:
            return None
        if len(connections) > 1:
            logger.warning(
                "Ambiguous connection lookup - user holds several accounts with this provider, "
                "returning the oldest; the caller should scope the work to one connection",
                extra={
                    "provider": provider,
                    "user_id": str(user_id),
                    "connection_count": len(connections),
                    "connection_id": str(connections[0].id),
                },
            )
        return connections[0]

    def get_active_connection(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
    ) -> UserConnection | None:
        """Get an active connection for a user and provider.

        Same scoping rules as :meth:`get_by_user_and_provider`; a scoped
        connection that is no longer active is reported as no connection.
        """
        scoped = self._scoped_connection(db_session, user_id, provider)
        if scoped is not None:
            return scoped if scoped.status == ConnectionStatus.ACTIVE else None

        connections = self._user_provider_query(db_session, user_id, provider, active_only=True).all()
        if not connections:
            return None
        if len(connections) > 1:
            logger.warning(
                "Ambiguous active connection lookup - user holds several accounts with this provider, "
                "returning the oldest",
                extra={
                    "provider": provider,
                    "user_id": str(user_id),
                    "connection_count": len(connections),
                    "connection_id": str(connections[0].id),
                },
            )
        return connections[0]

    def get_by_account_email(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        account_email: str,
    ) -> UserConnection | None:
        """Find a user's account with one provider by its login e-mail.

        Case-insensitive: providers echo back whatever casing the person typed
        when they signed up, and "P01@lab.edu" and "p01@lab.edu" are one account.
        """
        return (
            self._user_provider_query(db_session, user_id, provider)
            .filter(func.lower(self.model.account_email) == account_email.strip().lower())
            .first()
        )

    def _active_by_provider_external_id(
        self, db_session: DbSession, provider: str, provider_user_id: str
    ) -> Query[UserConnection]:
        """Base query: active connections for a given (provider, provider_user_id) pair.

        Ordered by created_at asc, id asc so the oldest connection is always
        index 0 — stable primary attribution in webhook fan-out across query
        plans and restarts.
        """
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.provider == provider,
                    self.model.provider_user_id == provider_user_id,
                    self.model.status == ConnectionStatus.ACTIVE,
                )
            )
            .order_by(self.model.created_at.asc(), self.model.id.asc())
        )

    def get_all_by_provider_user_id(
        self,
        db_session: DbSession,
        provider: str,
        provider_user_id: str,
    ) -> list[UserConnection]:
        """Get all active connections sharing the same external provider account.

        Used for multi-account sync fan-out: one provider account connected to
        several OpenWearables profiles.
        """
        return self._active_by_provider_external_id(db_session, provider, provider_user_id).all()

    def get_by_provider_user_id(
        self,
        db_session: DbSession,
        provider: str,
        provider_user_id: str,
    ) -> UserConnection | None:
        """Get connection by provider and provider's user ID.

        Useful for webhook processing where we receive provider's user ID
        and need to find our internal user.
        """
        try:
            return self._active_by_provider_external_id(db_session, provider, provider_user_id).one_or_none()
        except MultipleResultsFound:
            logger.warning(
                "Multiple active connections found for provider_user_id — returning first",
                extra={"provider": provider, "provider_user_id": provider_user_id},
            )
            return self._active_by_provider_external_id(db_session, provider, provider_user_id).first()

    def get_by_provider_username(
        self,
        db_session: DbSession,
        provider: str,
        provider_username: str,
    ) -> UserConnection | None:
        """Get connection by provider and provider's display username.

        Used by Suunto webhooks — the ``username`` field in the payload matches
        the ``user`` JWT claim stored as ``provider_username``.
        """
        try:
            return (
                db_session.query(self.model)
                .filter(
                    and_(
                        self.model.provider == provider,
                        self.model.provider_username == provider_username,
                        self.model.status == ConnectionStatus.ACTIVE,
                    ),
                )
                .one_or_none()
            )
        except MultipleResultsFound:
            logger.warning(
                "Multiple active connections found for provider_username — returning first",
                extra={"provider": provider, "provider_username": provider_username},
            )
            return (
                db_session.query(self.model)
                .filter(
                    and_(
                        self.model.provider == provider,
                        self.model.provider_username == provider_username,
                        self.model.status == ConnectionStatus.ACTIVE,
                    ),
                )
                .first()
            )

    def get_linked_user_ids(
        self,
        db_session: DbSession,
        exclude_user_id: UUID,
        provider_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], list[UUID]]:
        """For a list of (provider, provider_user_id) pairs, return other active OW users
        sharing the same external account, grouped by pair."""
        if not provider_pairs:
            return {}
        rows = (
            db_session.query(self.model.provider, self.model.provider_user_id, self.model.user_id)
            .filter(
                and_(
                    self.model.status == ConnectionStatus.ACTIVE,
                    self.model.user_id != exclude_user_id,
                    tuple_(self.model.provider, self.model.provider_user_id).in_(provider_pairs),
                )
            )
            .all()
        )
        result: dict[tuple[str, str], list[UUID]] = {}
        for provider, provider_user_id, linked_user_id in rows:
            result.setdefault((provider, provider_user_id), []).append(linked_user_id)
        return result

    def get_by_user_id(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> list[UserConnection]:
        """Get all connections for a specific user."""
        return (
            db_session.query(self.model)
            .filter(self.model.user_id == user_id)
            .order_by(self.model.created_at.desc())
            .all()
        )

    def get_expiring_tokens(self, db_session: DbSession, minutes_threshold: int = 5) -> list[UserConnection]:
        """Get connections with tokens expiring soon (for background refresh)."""
        now = datetime.now(timezone.utc)

        threshold_time = now + timedelta(minutes=minutes_threshold)

        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.status == ConnectionStatus.ACTIVE,
                    self.model.token_expires_at <= threshold_time,
                ),
            )
            .all()
        )

    def disconnect(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        connection_id: UUID | None = None,
    ) -> int:
        """Revoke a user's connection(s) to a provider. Returns rows updated.

        With ``connection_id`` this revokes that one account. Without it, every
        account the user holds with the provider is revoked - which is what the
        provider-level endpoint has always meant, and stays the right reading of
        "disconnect me from Garmin".
        """
        conditions = [
            UserConnection.user_id == user_id,
            UserConnection.provider == provider,
            UserConnection.status != ConnectionStatus.REVOKED,
        ]
        if connection_id is not None:
            conditions.append(UserConnection.id == connection_id)
        result = cast(
            CursorResult[tuple[()]],
            db_session.execute(
                update(UserConnection)
                .where(and_(*conditions))
                .values(
                    status=ConnectionStatus.REVOKED,
                    access_token=None,
                    refresh_token=None,
                    token_expires_at=None,
                    updated_at=datetime.now(timezone.utc),
                ),
            ),
        )
        db_session.commit()
        return result.rowcount

    def mark_as_revoked(self, db_session: DbSession, connection: UserConnection) -> UserConnection:
        """Mark connection as revoked (when refresh token fails)."""
        connection.status = ConnectionStatus.REVOKED
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_scope(self, db_session: DbSession, connection: UserConnection, scope: str | None) -> UserConnection:
        """Update connection scope (e.g. when user changes permissions on Garmin Connect)."""
        connection.scope = scope
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_tokens(
        self,
        db_session: DbSession,
        connection: UserConnection,
        access_token: str,
        refresh_token: str | None,
        expires_in: int,
    ) -> UserConnection:
        """Update connection with new tokens after refresh."""

        connection.access_token = access_token
        if refresh_token:
            connection.refresh_token = refresh_token
        connection.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_connection_info(
        self,
        db_session: DbSession,
        connection: UserConnection,
        access_token: str,
        refresh_token: str | None,
        expires_in: int,
        provider_user_id: str | None = None,
        provider_username: str | None = None,
        scope: str | None = None,
        account_email: str | None = None,
        account_label: str | None = None,
    ) -> UserConnection:
        """Update connection with new tokens and user info.

        ``account_email`` overwrites what was stored: it comes from the account
        that just authorised, so if it disagrees with the old value the old
        value is wrong. ``account_label`` is the opposite - it is the operator's
        note, and a reconnect must not quietly rename an account somebody named.
        """
        connection.access_token = access_token
        if refresh_token:
            connection.refresh_token = refresh_token
        connection.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        if provider_user_id and not connection.provider_user_id:
            connection.provider_user_id = provider_user_id
        if provider_username and not connection.provider_username:
            connection.provider_username = provider_username
        if scope and connection.scope != scope:
            connection.scope = scope
        if account_email:
            connection.account_email = account_email.strip() or None
        if account_label and not connection.account_label:
            connection.account_label = account_label

        connection.status = ConnectionStatus.ACTIVE
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_account_metadata(
        self,
        db_session: DbSession,
        connection: UserConnection,
        *,
        account_label: str | None = None,
        account_email: str | None = None,
        device_label: str | None = None,
        clear_account_label: bool = False,
        clear_account_email: bool = False,
        clear_device_label: bool = False,
    ) -> UserConnection:
        """Rename an account or correct the e-mail / device recorded against it.

        Clearing is explicit rather than "passing None", because None is also
        what a partial update sends for the fields it is not touching, and an
        e-mail silently blanked by an unrelated rename is exactly the kind of
        provenance loss this whole feature exists to prevent.
        """
        if account_label is not None or clear_account_label:
            connection.account_label = None if clear_account_label else account_label
        if account_email is not None or clear_account_email:
            connection.account_email = None if clear_account_email else (account_email or "").strip() or None
        if device_label is not None or clear_device_label:
            connection.device_label = None if clear_device_label else device_label
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def update_last_synced_at(self, db_session: DbSession, connection: UserConnection) -> UserConnection:
        """Update the last synced timestamp."""
        connection.last_synced_at = datetime.now(timezone.utc)
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection

    def get_all_active_by_user(self, db_session: DbSession, user_id: UUID) -> list[UserConnection]:
        """Get all active connections for a specific user."""
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.user_id == user_id,
                    self.model.status == ConnectionStatus.ACTIVE,
                ),
            )
            .all()
        )

    def get_all_active_by_provider(self, db_session: DbSession, provider: str) -> list[UserConnection]:
        return (
            db_session.query(self.model)
            .filter(
                and_(
                    self.model.provider == provider,
                    self.model.status == ConnectionStatus.ACTIVE,
                ),
            )
            .all()
        )

    def get_all_active_users(self, db_session: DbSession) -> list[UUID]:
        """Get all unique user IDs that have active connections."""
        return [
            row.user_id
            for row in db_session.query(self.model.user_id)
            .filter(self.model.status == ConnectionStatus.ACTIVE)
            .distinct()
            .all()
        ]

    def ensure_sdk_connection(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        account_email: str | None = None,
        account_label: str | None = None,
    ) -> tuple[UserConnection, SdkConnectionOutcome]:
        """Ensure an SDK-based connection exists for a user and provider.

        SDK-based providers (like Apple Health) don't use OAuth tokens.
        This method creates or returns an existing connection without tokens.

        ``account_email`` addresses one of several SDK accounts a user may hold
        with the same provider - two phones signed into two Apple IDs, say. When
        it is given, it selects the account outright, and a new one is created if
        no account with that e-mail exists yet. Without it the behaviour is
        unchanged: the user's existing connection, or a first one.

        Returns the connection and which branch was taken, so the caller can emit
        ``connection.created`` only on a real state change. The upload path calls
        this on every batch, so EXISTING must stay silent.
        """
        if account_email:
            existing = self.get_by_account_email(db_session, user_id, provider, account_email)
        else:
            existing = self.get_by_user_and_provider(db_session, user_id, provider)
        if existing:
            # Reactivate if revoked
            if existing.status != ConnectionStatus.ACTIVE:
                existing.status = ConnectionStatus.ACTIVE
                existing.updated_at = datetime.now(timezone.utc)
                db_session.add(existing)
                db_session.commit()
                db_session.refresh(existing)
                return existing, SdkConnectionOutcome.REACTIVATED
            return existing, SdkConnectionOutcome.EXISTING

        # Create new SDK connection (no tokens needed)
        connection = UserConnection(
            id=uuid4(),
            user_id=user_id,
            provider=provider,
            account_email=account_email.strip() if account_email else None,
            account_label=account_label,
            access_token=None,
            refresh_token=None,
            token_expires_at=None,
            status=ConnectionStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return connection, SdkConnectionOutcome.CREATED
