from logging import getLogger
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, asc, delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.elements import ColumnElement

from app.database import DbSession
from app.models import DataSource, HealthScore, ProviderPriority, UserConnection
from app.repositories.provider_priority_repository import ProviderPriorityRepository
from app.repositories.repositories import CrudRepository
from app.schemas.enums import DeviceType, ProviderName, infer_device_type_from_model, infer_device_type_from_source_name
from app.schemas.model_crud.data_priority import DataSourceCreate, DataSourceUpdate
from app.utils.connection_context import get_active_connection_id
from app.utils.device_registry import (
    PROVIDER_BRANDS,
    humanize_device_model,
    looks_like_writer_id,
    resolve_brand_signal,
)

if TYPE_CHECKING:
    from app.services.devices.identity import IdentityClaim

log = getLogger(__name__)


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
        user_connection_id: UUID | None = None,
    ) -> ColumnElement[bool]:
        conditions = [
            self.model.user_id == user_id,
            self.model.provider == provider,
            func.coalesce(self.model.device_model, "") == (device_model or ""),
            func.coalesce(self.model.source, "") == (source or ""),
        ]
        # The connection is part of the identity: two accounts with one provider
        # report identical device models, and pooling them would be irreversible.
        if user_connection_id is None:
            conditions.append(self.model.user_connection_id.is_(None))
        else:
            conditions.append(self.model.user_connection_id == user_connection_id)
        return and_(*conditions)

    def get_by_connection_identity(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_model: str | None = None,
        source: str | None = None,
        user_connection_id: UUID | None = None,
    ) -> DataSource | None:
        """The one data source for this identity *and* this account.

        ``user_connection_id=None`` means the connection-less row (a one-time
        import), not "any connection" - use :meth:`get_by_identity` for that.
        """
        return (
            db_session.query(self.model)
            .filter(self._build_identity_filter(user_id, provider, device_model, source, user_connection_id))
            .one_or_none()
        )

    def get_by_identity(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_model: str | None = None,
        source: str | None = None,
    ) -> DataSource | None:
        """A data source matching this identity, whichever account it came through.

        For callers that hold no connection of their own. With several accounts
        the identity no longer names one row, so the oldest is returned - a
        stable choice rather than a correct one, which is why ingest itself uses
        :meth:`get_by_connection_identity` instead.
        """
        conditions = [
            self.model.user_id == user_id,
            self.model.provider == provider,
            func.coalesce(self.model.device_model, "") == (device_model or ""),
            func.coalesce(self.model.source, "") == (source or ""),
        ]
        return (
            db_session.query(self.model)
            .filter(and_(*conditions))
            .order_by(self.model.created_at.asc(), self.model.id.asc())
            .first()
        )

    def _connection_device_label(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        user_connection_id: UUID | None = None,
    ) -> str | None:
        """The manually-set or auto-derived device label behind a connection.

        Used to fill device_model when the provider reports none. Scoped to one
        connection when the caller knows which account it is ingesting for -
        without that, a user with two Whoops would stamp both units with
        whichever label happened to come back first.
        """
        query = db_session.query(UserConnection.device_label).filter(
            UserConnection.user_id == user_id,
            UserConnection.provider == provider.value,
            UserConnection.device_label.isnot(None),
        )
        if user_connection_id is not None:
            query = query.filter(UserConnection.id == user_connection_id)
        return query.order_by(UserConnection.created_at.asc()).limit(1).scalar()

    def _connection_sensor_label(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        user_connection_id: UUID | None = None,
    ) -> str | None:
        """A sensor a person declared was worn under whatever the provider named.

        Used only to classify the data source, never to overwrite the provider's
        reported model - that stays in device_model verbatim, and the device registry
        is where the two are told apart (detection._create_relayed).

        Scoped to one connection for the same reason the device label is: a user with
        a reference strap on one account and none on another must not have the first
        account's declaration applied to the second.
        """
        query = db_session.query(UserConnection.sensor_label).filter(
            UserConnection.user_id == user_id,
            UserConnection.provider == provider.value,
            UserConnection.sensor_label.isnot(None),
        )
        if user_connection_id is not None:
            query = query.filter(UserConnection.id == user_connection_id)
        return query.order_by(UserConnection.created_at.asc()).limit(1).scalar()

    def set_connection_device_label(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_label: str | None,
        user_connection_id: UUID | None = None,
    ) -> int:
        """Retroactively relabel a user's existing device-less data sources.

        Sets device_model = device_label for this user/provider's data sources
        that currently have no device_model, so already-ingested data picks up
        the label too (future data is handled by ensure_data_source). Returns the
        number of rows updated.

        With ``user_connection_id`` only that account's data sources are
        relabelled, plus any that predate the connection being recorded
        (user_connection_id IS NULL) when the user holds a single account - see
        the caller in user_connection_service.
        """
        if not device_label:
            return 0
        query = db_session.query(self.model).filter(
            self.model.user_id == user_id,
            self.model.provider == provider,
            self.model.device_model.is_(None),
        )
        if user_connection_id is not None:
            query = query.filter(self.model.user_connection_id == user_connection_id)
        return query.update(
            {self.model.device_model: device_label},
            synchronize_session=False,
        )

    @staticmethod
    def _resolve_original_source_name(
        provider: ProviderName,
        device_model: str | None,
        source: str | None,
        caller_value: str | None,
    ) -> str | None:
        """The brand or app to file this source under.

        Non-destructive tagging: name the maker (e.g. Oura data arriving via Apple
        Health) so one brand groups across ingest paths.

        A brand the tables actually recognise wins over whatever the caller passed,
        because the one caller that supplies a value (event_record_repository) hands us
        the raw `creator.source`, which may be a package id like "com.oura.oura" rather
        than a brand. Honouring that first would put a literal where consumers expect
        "Oura", and this value feeds both device-type inference and
        resolve_ingestion_route(), which compares it against the platform brand.

        When no table matched, a readable caller value is kept. Falling straight
        through to the platform brand is what made every unrecognised HealthKit writer
        - Muse, AutoSleep, Eight Sleep, Hume - read as "Apple": it overwrote the only
        field naming the recorder, and made a relay read as first-party Apple data.

        A bare identifier ("com.some.app") yields to the platform brand, which is the
        case the fallback was written for - but only when there *is* one. For a
        provider with no brand of its own, storing the identifier beats storing NULL,
        since nothing else on the row names the writer.
        """
        matched = resolve_brand_signal(provider, device_model, source)
        if matched:
            return matched

        readable = [c.strip() for c in (caller_value, source) if c and c.strip() and not looks_like_writer_id(c)]
        if readable:
            return readable[0]

        platform_brand = PROVIDER_BRANDS.get(provider)
        if platform_brand:
            return platform_brand

        for candidate in (caller_value, source):
            if candidate and candidate.strip():
                return candidate.strip()
        return None

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
        identity_claims: "list[IdentityClaim] | None" = None,
    ) -> DataSource:
        # Fall back to the account the current unit of work declared. Most
        # provider paths never learned to pass a connection id - they were
        # written when (user, provider) named exactly one - so without this the
        # data source would be created connection-less and two accounts with the
        # same provider would share it. A one-time import (XML) runs in no scope,
        # so it stays NULL, which is what it should be.
        if user_connection_id is None:
            user_connection_id = get_active_connection_id()

        # Fill device_model from the connection's device_label when the provider
        # didn't report a device (e.g. Whoop, which exposes none; Oura, auto-filled
        # from ring_configuration). Manual entry / auto-detection both land here.
        if device_model is None:
            device_model = self._connection_device_label(db_session, user_id, provider, user_connection_id)

        # A sensor the account declared. It never replaces device_model - the
        # provider's report is kept verbatim, and the registry is where recorder and
        # sensor are separated - but it does decide what kind of device this source
        # is, because device_type is what ranks sources against each other. Left to
        # the reported model, an ECG strap recorded through a watch is classified as a
        # watch, and the reference instrument ends up ranked below the wrist sensors
        # it exists to be compared against.
        declared_sensor = self._connection_sensor_label(db_session, user_id, provider, user_connection_id)

        original_source_name = self._resolve_original_source_name(provider, device_model, source, original_source_name)

        existing = self.get_by_connection_identity(
            db_session, user_id, provider, device_model, source, user_connection_id
        )
        if existing is None and user_connection_id is not None:
            # Adopt a source ingested before connections were recorded against
            # them (an XML import, or any row predating this column) rather than
            # creating a duplicate beside it. Only ever done when the user holds
            # a single account with the provider: with two, there is no way to
            # tell which of them the orphaned rows came from, and guessing would
            # file one unit's history under the other.
            existing = self._adoptable_orphan(db_session, user_id, provider, device_model, source)

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
                device_type = self._infer_device_type(device_model, original_source_name, source, declared_sensor)
                object.__setattr__(existing, "device_type", device_type.value)
                updated = True
            if updated:
                db_session.flush()
            self._attribute_device(db_session, existing, identity_claims)
            return existing

        provider_priority_repo = ProviderPriorityRepository(ProviderPriority)
        provider_priority_repo.ensure_provider_exists(db_session, provider)

        device_type = self._infer_device_type(device_model, original_source_name, source, declared_sensor)

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
        self._attribute_device(db_session, result, identity_claims)
        return result

    def _can_adopt_orphans(self, db_session: DbSession, user_id: UUID, provider: ProviderName) -> bool:
        """Whether connection-less rows can safely be claimed by this user's account.

        Only when there is exactly one account to claim them for. With two, the
        rows could have come from either, and a wrong adoption silently merges
        two units' histories - which, unlike a wrong split, cannot be undone.
        """
        connection_count = (
            db_session.query(func.count(UserConnection.id))
            .filter(UserConnection.user_id == user_id, UserConnection.provider == provider.value)
            .scalar()
            or 0
        )
        return connection_count <= 1

    def _adoptable_orphan(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_model: str | None,
        source: str | None,
    ) -> DataSource | None:
        """A matching connection-less data source, when adopting it is unambiguous."""
        if not self._can_adopt_orphans(db_session, user_id, provider):
            return None
        return (
            db_session.query(self.model)
            .filter(self._build_identity_filter(user_id, provider, device_model, source, None))
            .one_or_none()
        )

    def _attribute_device(
        self,
        db_session: DbSession,
        data_source: DataSource,
        identity_claims: "list[IdentityClaim] | None" = None,
    ) -> None:
        """Point a data source at a physical device, best effort.

        Attribution is an enrichment on top of ingest, not part of it. A data source
        whose device cannot be worked out is still a perfectly good data source - it
        just waits for a later sync that carries a signal, or for someone to link it
        by hand - so a failure here must never cost the batch that was being written.
        The exception is logged rather than swallowed silently.

        Imported inside the function: app.services.devices.detection imports models and
        app.utils.device_registry, and a module-level import here would close a cycle
        through app.repositories.
        """
        from app.services.devices.detection import DeviceDetectionService

        try:
            DeviceDetectionService().resolve_for_data_source(db_session, data_source, identity_claims)
        except Exception:
            # getattr with a default, because this handler exists to guarantee that
            # attribution cannot cost the batch - and a handler that reads attributes
            # off the object that just failed can raise from inside the rescue, which
            # is the one way to lose the data it was written to protect.
            log.exception(
                "device attribution failed for data_source %s (provider=%s); left unattributed",
                getattr(data_source, "id", None),
                getattr(data_source, "provider", None),
            )

    # Types that name a body-worn recorder, as opposed to a handset or an app.
    # Types that name something worn, and so outrank a relayed handset's model.
    # EEG and HEADBAND belong here for the same reason the other four do: a sleep
    # headband reaches the platform only by relay - it has no API of its own - so
    # leaving them out meant the writing app's label could never beat the syncing
    # iPhone, and the one device type the priority table ranks first for sleep was
    # filed as a phone on every sync.
    _WEARABLE_TYPES: frozenset[DeviceType] = frozenset(
        {
            DeviceType.WATCH,
            DeviceType.BAND,
            DeviceType.RING,
            DeviceType.CHEST_STRAP,
            DeviceType.EEG,
            DeviceType.HEADBAND,
        }
    )

    def _infer_device_type(
        self,
        device_model: str | None,
        original_source_name: str | None,
        source: str | None = None,
        declared_sensor: str | None = None,
    ) -> DeviceType:
        """Classify a data source from the strongest device signal available.

        ``declared_sensor`` wins over everything: it is the one signal a person
        supplied rather than a provider, about the one thing no provider reports. It
        is consulted only when it names a recognisable type, so a free-text note
        ("reference strap") falls through to the inference below rather than
        flattening a perfectly good model string to UNKNOWN.

        ``source`` is the provider's own label for what recorded the sample - for
        HealthKit, the device name ("Michael's Apple Watch Ultra 3"). ``device_model``
        is the hardware code, which for Apple comes from ``productType`` and reports
        the handset that *synced* the batch rather than the device that took the
        reading: a watch's samples routinely arrive stamped "iPhone18,1". So when the
        model says phone and the label names something worn, the label wins. A real
        wearable model (Watch7,x) is never downgraded by this.
        """
        if declared_sensor:
            from_sensor = infer_device_type_from_source_name(declared_sensor)
            if from_sensor is not DeviceType.UNKNOWN:
                return from_sensor

        from_model = infer_device_type_from_model(device_model)

        if from_model == DeviceType.PHONE:
            from_source = infer_device_type_from_source_name(source)
            if from_source in self._WEARABLE_TYPES:
                return from_source

        if from_model != DeviceType.UNKNOWN:
            return from_model

        # No usable model: the label first, then the canonical brand as a last resort.
        from_source = infer_device_type_from_source_name(source)
        if from_source != DeviceType.UNKNOWN:
            return from_source
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

        # Same fallback as ensure_data_source: the bulk path is fed by provider
        # code that mostly does not carry a connection id of its own.
        if user_connection_id is None:
            user_connection_id = get_active_connection_id()

        identities_list = list(identities)

        from sqlalchemy import or_

        # Fill device_model from the connection's device_label when the provider didn't
        # report one, mirroring ensure_data_source. Without it, time series (which arrive
        # through this bulk path) land on a device-less source while events land on the
        # labelled one, splitting a single device across two data sources.
        label_cache: dict[UUID, str | None] = {}
        # Same reason, for the declared sensor: it decides device_type, and a time
        # series classified as a watch beside an event record classified as a chest
        # strap would rank the same unit two different ways.
        sensor_cache: dict[UUID, str | None] = {}

        def _declared_sensor(user_id: UUID) -> str | None:
            if user_id not in sensor_cache:
                sensor_cache[user_id] = self._connection_sensor_label(db_session, user_id, provider, user_connection_id)
            return sensor_cache[user_id]

        def _stored_device_model(user_id: UUID, device_model: str | None) -> str | None:
            if device_model is not None:
                return device_model
            if user_id not in label_cache:
                label_cache[user_id] = self._connection_device_label(db_session, user_id, provider, user_connection_id)
            return label_cache[user_id]

        # Callers look rows up by the identity they passed in, which may carry a null
        # device_model, so keep both: what was asked for, and what is actually stored.
        stored_by_requested: dict[tuple[UUID, str | None, str | None], tuple[UUID, str | None, str | None]] = {
            (user_id, device_model, source): (user_id, _stored_device_model(user_id, device_model), source)
            for user_id, device_model, source in identities_list
        }

        # Every identity in one call shares the same connection, so the filter
        # below is per-account and two accounts with one provider can no longer
        # collide on an identical (device_model, source).
        conditions = [
            self._build_identity_filter(user_id, provider, device_model, source, user_connection_id)
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

        # Adopt connection-less rows the same way ensure_data_source does, and
        # under the same condition: only when the user holds a single account
        # with this provider, so a row whose origin is unknowable is never
        # guessed onto one of two units. Without this the events path (which
        # adopts) and this time-series path (which would not) would file the
        # same device under two data sources - the exact split this method's
        # docstring exists to prevent.
        if missing and user_connection_id is not None:
            # Cached per user: a payload can carry dozens of identities and the
            # answer is the same for every one of them.
            adoption_allowed: dict[UUID, bool] = {}

            def _allowed(user_id: UUID) -> bool:
                if user_id not in adoption_allowed:
                    adoption_allowed[user_id] = self._can_adopt_orphans(db_session, user_id, provider)
                return adoption_allowed[user_id]

            adoptable = {
                (user_id, device_model, source) for user_id, device_model, source in missing if _allowed(user_id)
            }
            if adoptable:
                orphan_conditions = [
                    self._build_identity_filter(user_id, provider, device_model, source, None)
                    for user_id, device_model, source in adoptable
                ]
                orphans = db_session.query(self.model).filter(or_(*orphan_conditions)).all()
                for orphan in orphans:
                    object.__setattr__(orphan, "user_connection_id", user_connection_id)
                    ids_by_stored[(orphan.user_id, orphan.device_model, orphan.source)] = orphan.id
                if orphans:
                    db_session.flush()
                    result.update(
                        {
                            requested: ids_by_stored[stored]
                            for requested, stored in stored_by_requested.items()
                            if requested not in result and stored in ids_by_stored
                        }
                    )
                    missing = [stored for requested, stored in stored_by_requested.items() if requested not in result]

        if missing:
            values = []
            for user_id, device_model, source in missing:
                # Mirror ensure_data_source: derive the canonical brand here too, so rows
                # first created by the bulk path aren't left with a NULL brand, and always
                # store a device_type rather than NULL. Same rule as the single path,
                # including keeping a readable writer name the brand tables do not know -
                # otherwise one Muse source would read "Muse" and the other "Apple"
                # depending only on which path happened to create the row first.
                original_source_name = self._resolve_original_source_name(provider, device_model, source, None)
                device_type = self._infer_device_type(
                    device_model, original_source_name, source, _declared_sensor(user_id)
                )
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
                self._build_identity_filter(user_id, provider, device_model, source, user_connection_id)
                for user_id, device_model, source in missing
            ]

            newly_inserted = db_session.query(self.model).filter(or_(*conditions)).all()
            ids_by_stored.update({(ds.user_id, ds.device_model, ds.source): ds.id for ds in newly_inserted})

            # Attribute what this path created, mirroring ensure_data_source. Time
            # series arrive through here while events arrive through the single path,
            # so skipping it would leave one device's streams half attributed. The
            # loop is over distinct identities, not samples, so it stays small.
            for data_source in newly_inserted:
                self._attribute_device(db_session, data_source)

            result.update(
                {
                    requested: ids_by_stored[stored]
                    for requested, stored in stored_by_requested.items()
                    if requested not in result and stored in ids_by_stored
                }
            )

        return result

    def observed_devices_by_connection(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> dict[UUID, list[str]]:
        """The device models each of a user's accounts has actually reported.

        Automatic labelling from what the providers already send: Garmin names
        the watch on its activities, Apple ships an HKDevice, Samsung and Health
        Connect carry a model string. That value is on the data source the
        moment the first sample lands, so an account does not need anyone to
        type what is behind it - the accounts that do are the ones whose
        provider reports nothing (Whoop), and those still fall back to the
        device label set by hand.

        Returned humanised (marketing names for opaque hardware codes) and
        de-duplicated, ordered by how recently the source was created so the
        current device leads. Accounts with nothing observed are absent.
        """
        rows = (
            db_session.query(self.model.user_connection_id, self.model.device_model)
            .filter(
                self.model.user_id == user_id,
                self.model.user_connection_id.isnot(None),
                self.model.device_model.isnot(None),
            )
            .order_by(self.model.created_at.desc())
            .all()
        )
        observed: dict[UUID, list[str]] = {}
        for connection_id, device_model in rows:
            name = humanize_device_model(device_model) or device_model
            seen = observed.setdefault(connection_id, [])
            if name not in seen:
                seen.append(name)
        return observed

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

    def delete_user_provider_data(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
    ) -> int:
        """Delete all of a user's data for a single provider.

        Deletes the user's health_score rows for the provider (some are not linked to
        a data_source), then the data_source rows. ON DELETE CASCADE on the data_source
        FK removes every dependent row - event_records, data_point_series (+ archive),
        event/sleep/workout/menstrual details and health_scores linked via data_source
        or event_record. Returns the number of data_source rows deleted.
        """
        db_session.execute(
            delete(HealthScore).where(
                and_(HealthScore.user_id == user_id, HealthScore.provider == provider),
            ),
        )
        result = cast(
            CursorResult,
            db_session.execute(
                delete(self.model).where(
                    and_(self.model.user_id == user_id, self.model.provider == provider),
                ),
            ),
        )
        db_session.commit()
        return result.rowcount

    def delete_connection_data(
        self,
        db_session: DbSession,
        user_id: UUID,
        user_connection_id: UUID,
    ) -> int:
        """Delete the data ingested through one connected account.

        The provider-wide purge above takes every account with that provider
        along with it. This one is the scalpel: it removes the data sources
        carrying this connection id and, by cascade, everything hanging off them,
        and leaves the participant's other accounts with the same provider - the
        comparator device in a validation study - completely untouched.

        Health scores linked to those data sources go with them by cascade.
        Scores computed without one - ``data_source_id IS NULL``, which the
        provider-wide purge removes by (user, provider) - are deliberately left:
        they cannot be attributed to one of a user's several accounts, and
        deleting them would take the comparator account's scores too. The
        provider-wide purge is the way to clear those.
        """
        result = cast(
            CursorResult,
            db_session.execute(
                delete(self.model).where(
                    and_(
                        self.model.user_id == user_id,
                        self.model.user_connection_id == user_connection_id,
                    ),
                ),
            ),
        )
        db_session.commit()
        return result.rowcount

    def infer_provider_from_source(self, source: str | None) -> ProviderName:
        """Infer provider from source string.

        Deprecated: Use ProviderName.from_source_string() directly instead.
        This method is kept for backward compatibility.
        """
        return ProviderName.from_source_string(source)
