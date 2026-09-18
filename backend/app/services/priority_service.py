from logging import Logger, getLogger
from uuid import UUID

from app.database import DbSession
from app.models import DataSource, ProviderPriority, UserConnection
from app.repositories import DataSourceRepository, ProviderPriorityRepository
from app.repositories.device_type_priority_repository import DeviceTypePriorityRepository
from app.schemas.enums import DeviceType, ProviderName
from app.schemas.model_crud.data_priority import (
    DataSourceListResponse,
    DataSourceResponse,
    DeviceTypePriorityBulkUpdate,
    DeviceTypePriorityListResponse,
    DeviceTypePriorityResponse,
    ProviderPriorityBulkUpdate,
    ProviderPriorityListResponse,
    ProviderPriorityResponse,
)
from app.schemas.model_crud.user_management import account_display_label
from app.utils.device_registry import humanize_device_model, resolve_ingestion_route
from app.utils.exceptions import handle_exceptions


class PriorityService:
    def __init__(self, log: Logger):
        self.logger = log
        self.priority_repo = ProviderPriorityRepository(ProviderPriority)
        self.device_type_priority_repo = DeviceTypePriorityRepository()
        self.data_source_repo = DataSourceRepository(DataSource)

    @handle_exceptions
    def get_provider_priorities(
        self,
        db_session: DbSession,
    ) -> ProviderPriorityListResponse:
        priorities = self.priority_repo.get_all_ordered(db_session)
        return ProviderPriorityListResponse(items=[ProviderPriorityResponse.model_validate(p) for p in priorities])

    @handle_exceptions
    def update_provider_priority(
        self,
        db_session: DbSession,
        provider: ProviderName,
        priority: int,
    ) -> ProviderPriorityResponse:
        result = self.priority_repo.upsert(db_session, provider, priority)
        db_session.commit()
        return ProviderPriorityResponse.model_validate(result)

    @handle_exceptions
    def bulk_update_priorities(
        self,
        db_session: DbSession,
        update: ProviderPriorityBulkUpdate,
    ) -> ProviderPriorityListResponse:
        priorities_tuples = [(p.provider, p.priority) for p in update.priorities]
        results = self.priority_repo.bulk_update(db_session, priorities_tuples)
        db_session.commit()
        return ProviderPriorityListResponse(items=[ProviderPriorityResponse.model_validate(p) for p in results])

    @handle_exceptions
    def get_user_data_sources(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> DataSourceListResponse:
        sources = self.data_source_repo.get_user_data_sources(db_session, user_id)
        # One query for the user's accounts rather than a lazy load per source:
        # the listing is short, and a participant in a multi-device study has
        # several sources per account.
        user_accounts = db_session.query(UserConnection).filter(UserConnection.user_id == user_id).all()
        accounts = {connection.id: connection for connection in user_accounts}
        # How many accounts the user holds with each provider, so a name is only
        # qualified when it would otherwise be ambiguous.
        siblings: dict[str, int] = {}
        for connection in user_accounts:
            siblings[connection.provider] = siblings.get(connection.provider, 0) + 1
        items = [
            DataSourceResponse(
                id=ds.id,
                user_id=ds.user_id,
                provider=ds.provider,
                user_connection_id=ds.user_connection_id,
                device_model=ds.device_model,
                software_version=ds.software_version,
                source=ds.source,
                device_type=ds.device_type,
                original_source_name=ds.original_source_name,
                display_name=self._build_display_name(
                    ds,
                    self._account_suffix(
                        accounts.get(ds.user_connection_id),
                        siblings.get(str(getattr(ds.provider, "value", ds.provider)), 0),
                    ),
                ),
                device_id=ds.device_id,
                attribution_locked_at=ds.attribution_locked_at,
                account_label=self._account_label(accounts.get(ds.user_connection_id)),
                account_email=getattr(accounts.get(ds.user_connection_id), "account_email", None),
                account_type=getattr(accounts.get(ds.user_connection_id), "account_type", None),
                ingestion_route=resolve_ingestion_route(ds.provider, ds.original_source_name),
            )
            for ds in sources
        ]
        return DataSourceListResponse(items=items, total=len(items))

    @staticmethod
    def _account_label(connection: UserConnection | None) -> str | None:
        """The account's display name, or None when the source has no connection."""
        if connection is None:
            return None
        return account_display_label(
            connection.account_label,
            connection.provider_username,
            connection.account_email,
            connection.provider,
            connection.id,
        )

    @handle_exceptions
    def get_device_type_priorities(
        self,
        db_session: DbSession,
    ) -> DeviceTypePriorityListResponse:
        priorities = self.device_type_priority_repo.get_all_ordered(db_session)
        return DeviceTypePriorityListResponse(items=[DeviceTypePriorityResponse.model_validate(p) for p in priorities])

    @handle_exceptions
    def update_device_type_priority(
        self,
        db_session: DbSession,
        device_type: DeviceType,
        priority: int,
    ) -> DeviceTypePriorityResponse:
        result = self.device_type_priority_repo.upsert(db_session, device_type, priority)
        db_session.commit()
        return DeviceTypePriorityResponse.model_validate(result)

    @handle_exceptions
    def bulk_update_device_type_priorities(
        self,
        db_session: DbSession,
        update: DeviceTypePriorityBulkUpdate,
    ) -> DeviceTypePriorityListResponse:
        priorities_tuples = [(p.device_type, p.priority) for p in update.priorities]
        results = self.device_type_priority_repo.bulk_update(db_session, priorities_tuples)
        db_session.commit()
        return DeviceTypePriorityListResponse(items=[DeviceTypePriorityResponse.model_validate(p) for p in results])

    def get_priority_data_source_ids(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> list[UUID]:
        """Get data source IDs for a user, ordered by global priority."""
        provider_order = self.priority_repo.get_priority_order(db_session)
        device_type_order = self.device_type_priority_repo.get_priority_order(db_session)
        sources = self.data_source_repo.get_user_data_sources(db_session, user_id)

        if not sources:
            return []

        def sort_key(ds: DataSource) -> tuple[int, int, str]:
            provider_priority = provider_order.get(ds.provider, 99)
            device_type_priority = 99
            if ds.device_type:
                try:
                    dt = DeviceType(ds.device_type)
                    device_type_priority = device_type_order.get(dt, 99)
                except ValueError:
                    pass
            return (provider_priority, device_type_priority, ds.device_model or "")

        sorted_sources = sorted(sources, key=sort_key)
        return [ds.id for ds in sorted_sources]

    def get_best_data_source_id(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> UUID | None:
        ids = self.get_priority_data_source_ids(db_session, user_id)
        return ids[0] if ids else None

    def _build_display_name(self, ds: DataSource, account_suffix: str | None = None) -> str:
        parts = []
        if ds.provider:
            # Rows loaded from the database carry provider as a plain str (the
            # column is a String, not a native enum), so there is no .value.
            parts.append(ds.provider.capitalize())
        if ds.device_model:
            # Prefer a marketing name for opaque hardware codes; fall back to the
            # raw device_model when the code is unknown (never lost).
            parts.append(humanize_device_model(ds.device_model) or ds.device_model)
        elif ds.original_source_name:
            parts.append(ds.original_source_name)
        name = " - ".join(parts) if parts else "Unknown Source"
        # Two accounts with one provider report the same model, so "Garmin -
        # fenix 7" names both of them. Appended only when the user actually
        # holds several, so a single-account user's name is unchanged and every
        # existing consumer of this string sees what it saw before.
        return f"{name} ({account_suffix})" if account_suffix else name

    @staticmethod
    def _account_suffix(connection: UserConnection | None, sibling_count: int) -> str | None:
        """How to tell this account apart, when the user holds more than one.

        The classification comes first because it is what a study scans for -
        which of these is the validation unit - and the name or e-mail follows
        to separate two accounts that share a classification.
        """
        if connection is None or sibling_count < 2:
            return None
        bits: list[str] = []
        if connection.account_type:
            bits.append(str(connection.account_type))
        identifier = connection.account_label or connection.provider_username or connection.account_email
        if identifier:
            bits.append(identifier)
        if not bits:
            bits.append(f"account {str(connection.id)[:8]}")
        return " · ".join(bits)


priority_service = PriorityService(log=getLogger(__name__))
