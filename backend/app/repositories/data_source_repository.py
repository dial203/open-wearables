from uuid import UUID, uuid4

from sqlalchemy import and_, asc, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.elements import ColumnElement

from app.database import DbSession
from app.models import DataSource, ProviderPriority, UserConnection
from app.repositories.provider_priority_repository import ProviderPriorityRepository
from app.repositories.repositories import CrudRepository
from app.schemas.enums import DeviceType, ProviderName, infer_device_type_from_model, infer_device_type_from_source_name
from app.schemas.model_crud.data_priority import DataSourceCreate, DataSourceUpdate
from app.utils.device_registry import resolve_brand


class DataSourceRepository(
    CrudRepository[DataSource, DataSourceCreate, DataSourceUpdate],
):
    def __init__(self, model: type[DataSource] = DataSource):
        super().__init__(model)

    def _build_identity_filter(
        self,
        user_id: UUID,
        provider: ProviderName,
        device_model: str | None,
        source: str | None,
    ) -> ColumnElement[bool]:
        conditions = [
            self.model.user_id == user_id,
            self.model.provider == provider,
            func.coalesce(self.model.device_model, "") == (device_model or ""),
            func.coalesce(self.model.source, "") == (source or ""),
        ]
        return and_(*conditions)

    def get_by_identity(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_model: str | None = None,
        source: str | None = None,
    ) -> DataSource | None:
        return (
            db_session.query(self.model)
            .filter(self._build_identity_filter(user_id, provider, device_model, source))
            .one_or_none()
        )

    def _connection_device_label(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
    ) -> str | None:
        """The manually-set or auto-derived device label for a user's connection.

        Used to fill device_model when the provider reports none. One connection
        per (user, provider) is guaranteed by a unique index.
        """
        return (
            db_session.query(UserConnection.device_label)
            .filter(
                UserConnection.user_id == user_id,
                UserConnection.provider == provider.value,
                UserConnection.device_label.isnot(None),
            )
            .scalar()
        )

    def set_connection_device_label(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_label: str | None,
    ) -> int:
        """Retroactively relabel a user's existing device-less data sources.

        Sets device_model = device_label for this user/provider's data sources
        that currently have no device_model, so already-ingested data picks up
        the label too (future data is handled by ensure_data_source). Returns the
        number of rows updated.
        """
        if not device_label:
            return 0
        return (
            db_session.query(self.model)
            .filter(
                self.model.user_id == user_id,
                self.model.provider == provider,
                self.model.device_model.is_(None),
            )
            .update(
                {self.model.device_model: device_label},
                synchronize_session=False,
            )
        )

    def ensure_data_source(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        user_connection_id: UUID | None = None,
        device_model: str | None = None,
        software_version: str | None = None,
        source: str | None = None,
        original_source_name: str | None = None,
    ) -> DataSource:
        # Fill device_model from the connection's device_label when the provider
        # didn't report a device (e.g. Whoop, which exposes none; Oura, auto-filled
        # from ring_configuration). Manual entry / auto-detection both land here.
        if device_model is None:
            device_model = self._connection_device_label(db_session, user_id, provider)

        # Non-destructive brand tagging: when the provider did not supply an
        # original_source_name, derive a canonical brand (e.g. Oura data arriving
        # via Apple/Google Health) so the same brand groups across ingest paths.
        if original_source_name is None:
            original_source_name = resolve_brand(provider, device_model, source)

        existing = self.get_by_identity(db_session, user_id, provider, device_model, source)
        if existing:
            updated = False
            if user_connection_id and existing.user_connection_id is None:
                object.__setattr__(existing, "user_connection_id", user_connection_id)
                updated = True
            if software_version and existing.software_version is None:
                object.__setattr__(existing, "software_version", software_version)
                updated = True
            if original_source_name and existing.original_source_name is None:
                object.__setattr__(existing, "original_source_name", original_source_name)
                updated = True
            if existing.device_type is None:
                # Always store a value (including "unknown"): consumers key off
                # device_type to separate real wearables from phone/app relays, and a
                # NULL forces them back to guessing from model strings.
                device_type = self._infer_device_type(device_model, original_source_name)
                object.__setattr__(existing, "device_type", device_type.value)
                updated = True
            if updated:
                db_session.flush()
            return existing

        provider_priority_repo = ProviderPriorityRepository(ProviderPriority)
        provider_priority_repo.ensure_provider_exists(db_session, provider)

        device_type = self._infer_device_type(device_model, original_source_name)

        create_payload = DataSourceCreate(
            id=uuid4(),
            user_id=user_id,
            provider=provider,
            user_connection_id=user_connection_id,
            device_model=device_model,
            software_version=software_version,
            source=source,
            device_type=device_type.value,
            original_source_name=original_source_name,
        )
        result = self.create(db_session, create_payload)
        assert result is not None
        return result

    def _infer_device_type(
        self,
        device_model: str | None,
        original_source_name: str | None,
    ) -> DeviceType:
        dt = infer_device_type_from_model(device_model)
        if dt != DeviceType.UNKNOWN:
            return dt
        return infer_device_type_from_source_name(original_source_name)

    def batch_ensure_data_sources(
        self,
        db_session: DbSession,
        provider: ProviderName,
        user_connection_id: UUID | None,
        identities: set[tuple[UUID, str | None, str | None]],
    ) -> dict[tuple[UUID, str | None, str | None], UUID]:
        if not identities:
            return {}

        identities_list = list(identities)

        from sqlalchemy import or_

        # Fill device_model from the connection's device_label when the provider didn't
        # report one, mirroring ensure_data_source. Without it, time series (which arrive
        # through this bulk path) land on a device-less source while events land on the
        # labelled one, splitting a single device across two data sources.
        label_cache: dict[UUID, str | None] = {}

        def _stored_device_model(user_id: UUID, device_model: str | None) -> str | None:
            if device_model is not None:
                return device_model
            if user_id not in label_cache:
                label_cache[user_id] = self._connection_device_label(db_session, user_id, provider)
            return label_cache[user_id]

        # Callers look rows up by the identity they passed in, which may carry a null
        # device_model, so keep both: what was asked for, and what is actually stored.
        stored_by_requested: dict[tuple[UUID, str | None, str | None], tuple[UUID, str | None, str | None]] = {
            (user_id, device_model, source): (user_id, _stored_device_model(user_id, device_model), source)
            for user_id, device_model, source in identities_list
        }

        conditions = [
            self._build_identity_filter(user_id, provider, device_model, source)
            for user_id, device_model, source in stored_by_requested.values()
        ]

        existing = db_session.query(self.model).filter(or_(*conditions)).all()
        ids_by_stored: dict[tuple[UUID, str | None, str | None], UUID] = {
            (ds.user_id, ds.device_model, ds.source): ds.id for ds in existing
        }

        result: dict[tuple[UUID, str | None, str | None], UUID] = {
            requested: ids_by_stored[stored]
            for requested, stored in stored_by_requested.items()
            if stored in ids_by_stored
        }

        missing = [stored for requested, stored in stored_by_requested.items() if requested not in result]

        if missing:
            values = []
            for user_id, device_model, source in missing:
                # Mirror ensure_data_source: derive the canonical brand here too, so rows
                # first created by the bulk path aren't left with a NULL brand, and always
                # store a device_type rather than NULL.
                original_source_name = resolve_brand(provider, device_model, source)
                device_type = self._infer_device_type(device_model, original_source_name)
                values.append(
                    {
                        "id": uuid4(),
                        "user_id": user_id,
                        "provider": provider,
                        "user_connection_id": user_connection_id,
                        "device_model": device_model,
                        "source": source,
                        "device_type": device_type.value,
                        "original_source_name": original_source_name,
                    }
                )
            stmt = insert(self.model).values(values).on_conflict_do_nothing()
            db_session.execute(stmt)
            db_session.flush()

            conditions = [
                self._build_identity_filter(user_id, provider, device_model, source)
                for user_id, device_model, source in missing
            ]

            newly_inserted = db_session.query(self.model).filter(or_(*conditions)).all()
            ids_by_stored.update({(ds.user_id, ds.device_model, ds.source): ds.id for ds in newly_inserted})

            result.update(
                {
                    requested: ids_by_stored[stored]
                    for requested, stored in stored_by_requested.items()
                    if requested not in result and stored in ids_by_stored
                }
            )

        return result

    def get_user_data_sources(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> list[DataSource]:
        return (
            db_session.query(self.model)
            .filter(self.model.user_id == user_id)
            .order_by(asc(self.model.provider), asc(self.model.device_model))
            .all()
        )

    def infer_provider_from_source(self, source: str | None) -> ProviderName:
        """Infer provider from source string.

        Deprecated: Use ProviderName.from_source_string() directly instead.
        This method is kept for backward compatibility.
        """
        return ProviderName.from_source_string(source)
