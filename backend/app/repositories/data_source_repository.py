import contextlib
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from logging import getLogger
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, and_, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.sql.elements import ColumnElement

from app.constants.devices_map import (
    HANDSET_DEVICE_TYPES,
    infer_device_type_from_model,
    infer_device_type_from_source_name,
    is_host_model,
    reconcile_device_type,
)
from app.constants.devices_map.device_types import SINGLE_DEVICE_PROVIDER_TYPE
from app.constants.sdk_providers import SDK_PROVIDERS
from app.database import DbSession
from app.models import DataSource, HealthScore, ProviderPriority, UserConnection
from app.repositories.data_type_coverage_repository import data_type_coverage_repository
from app.repositories.provider_priority_repository import ProviderPriorityRepository
from app.repositories.repositories import CrudRepository
from app.schemas.enums import (
    DeviceModelOrigin,
    DeviceType,
    ProviderName,
)
from app.schemas.model_crud.data_priority import DataSourceCreate, DataSourceUpdate
from app.utils.connection_context import get_active_connection_id
from app.utils.device_registry import (
    PROVIDER_BRANDS,
    humanize_device_model,
    is_handset_model,
    looks_like_writer_id,
    resolve_brand_signal,
)
from app.utils.device_timeline import load_timeline

if TYPE_CHECKING:
    from app.services.devices.identity import IdentityClaim

log = getLogger(__name__)

# Creator fields that describe the record's data source rather than the record itself.
# The writers strip them before building the row (upstream #1781's DATA_SOURCE_FIELDS,
# with the fork's provider_device_id in place of upstream's device_id).
SOURCE_ID_FIELDS = ("provider_device_id", "source_app_id", "device_manufacturer")

# Columns the bulk path inserts, in one statement for every row it creates.
_INSERT_FIELDS = (
    "id",
    "user_id",
    "provider",
    "user_connection_id",
    "device_model",
    "source",
    "device_type",
    "original_source_name",
    "device_model_origin",
    "provider_device_id",
    "source_app_id",
    "device_manufacturer",
)


def _version_key(version: str) -> tuple[int, ...] | None:
    """Dotted ASCII numbers, else None; never raises, so an odd version only skips the update."""
    parts = version.split(".")
    if not all(part.isascii() and part.isdigit() for part in parts):
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:  # past Python's digit limit for int()
        return None


def _is_newer_version(candidate: str, current: str | None) -> bool:
    """A history sync replays old versions in any order, so only a higher one replaces the stored one (#1873)."""
    new = _version_key(candidate)
    if new is None:
        return current is None
    old = _version_key(current) if current is not None else None
    return old is None or new > old


class DataSourceIdentity(NamedTuple):
    """What a data source is matched on within one account.

    The first three fields are the fork's original key. The two ids are upstream #1781's
    stable identifiers - the provider's id for the physical unit and the writing app's id
    - and take precedence when present, so two same-model units on one account stay
    apart and a writer's rows stay together whatever it calls its source. A plain
    (user_id, device_model, source) tuple still works anywhere one of these is accepted.
    """

    user_id: UUID
    device_model: str | None
    source: str | None
    provider_device_id: str | None = None
    source_app_id: str | None = None

    def with_clean_ids(self) -> "DataSourceIdentity":
        """Blank or whitespace-only ids count as absent."""
        return self._replace(
            provider_device_id=(self.provider_device_id or "").strip() or None,
            source_app_id=(self.source_app_id or "").strip() or None,
        )

    @property
    def keyed(self) -> bool:
        return bool(self.provider_device_id or self.source_app_id)

    def legacy(self) -> "DataSourceIdentity":
        """The same identity without its ids: how a row written before the ids arrived is keyed."""
        return self._replace(provider_device_id=None, source_app_id=None)


class BulkSourceCreator(Protocol):
    """What the bulk writers' creators share: enough to name their data source."""

    user_id: UUID
    provider: str | None
    user_connection_id: UUID | None
    device_model: str | None
    source: str | None


def creator_identity(creator: BulkSourceCreator, device_model: str | None = None) -> DataSourceIdentity:
    """A creator's identity, with ``device_model`` standing in for its own when given."""
    return DataSourceIdentity(
        creator.user_id,
        device_model if device_model is not None else creator.device_model,
        creator.source,
        getattr(creator, "provider_device_id", None),
        getattr(creator, "source_app_id", None),
    ).with_clean_ids()


_Creator = TypeVar("_Creator", bound=BulkSourceCreator)


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
            # The model + source tier only: a row keyed by a stable id can share its
            # model and source with a legacy row (the partial unique indexes allow it),
            # and is found through _match instead.
            self.model.provider_device_id.is_(None),
            self.model.source_app_id.is_(None),
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

    def find_for_identity(
        self,
        db_session: DbSession,
        provider: ProviderName,
        identity: DataSourceIdentity,
        user_connection_id: UUID | None,
    ) -> DataSource | None:
        """The row ingest would file ``identity`` under on this account, without creating or stamping it."""
        identity = DataSourceIdentity(*identity).with_clean_ids()
        return self._match(
            self._account_rows(db_session, provider, {identity.user_id}, user_connection_id),
            identity,
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

    def _account_rows(
        self,
        db_session: DbSession,
        provider: ProviderName,
        user_ids: set[UUID],
        user_connection_id: UUID | None,
    ) -> list[DataSource]:
        """Every row of these users for one provider on one account (None: the connection-less rows).

        A user has a handful of sources per provider, so matching runs over them in memory.
        """
        connection = (
            self.model.user_connection_id.is_(None)
            if user_connection_id is None
            else self.model.user_connection_id == user_connection_id
        )
        return (
            db_session.query(self.model)
            .filter(self.model.provider == provider, self.model.user_id.in_(user_ids), connection)
            .all()
        )

    @staticmethod
    def _find(rows: Sequence[DataSource], identity: DataSourceIdentity) -> DataSource | None:
        """The row on the identity's own tier: device id, else app id + model, else model + source."""
        for row in rows:
            if row.user_id != identity.user_id:
                continue
            if identity.provider_device_id:
                matched = row.provider_device_id == identity.provider_device_id
            elif identity.source_app_id:
                matched = (
                    row.provider_device_id is None
                    and row.source_app_id == identity.source_app_id
                    and (row.device_model or "") == (identity.device_model or "")
                )
            else:
                matched = (
                    row.provider_device_id is None
                    and row.source_app_id is None
                    and (row.device_model or "") == (identity.device_model or "")
                    and (row.source or "") == (identity.source or "")
                )
            if matched:
                return row
        return None

    @classmethod
    def _match(cls, rows: Sequence[DataSource], identity: DataSourceIdentity) -> DataSource | None:
        """The existing row for an identity, from rows already scoped to its account.

        Upstream #1781's order, minus its host-model rewrite (which replaces a stored
        device_model, and here a stored model is a capture with a recorded origin):

        1. A record sent without ids joins the one keyed row with its model, so a route
           that sends ids on some records only (Samsung step counts) keeps one source.
        2. The identity's own tier.
        3. A writer's only row when the model is missing on one side, so a writer's
           records with and without a model converge. Missing means None here, not an
           Apple host code as upstream also counts it - see above.
        4. A row written before the ids were known: the app-id row for an identity that
           now also carries a device id, else the model + source row. The caller stamps
           the ids onto it, so a device's history stays on the source it started on.
        """
        if not identity.keyed and identity.device_model:
            keyed = [
                r
                for r in rows
                if r.user_id == identity.user_id
                and (r.provider_device_id or r.source_app_id)
                and r.device_model == identity.device_model
            ]
            if len(keyed) == 1:
                return keyed[0]
        if found := cls._find(rows, identity):
            return found
        if identity.source_app_id and not identity.provider_device_id:
            writer_rows = [
                r
                for r in rows
                if r.user_id == identity.user_id
                and r.provider_device_id is None
                and r.source_app_id == identity.source_app_id
            ]
            if len(writer_rows) == 1 and (identity.device_model is None or writer_rows[0].device_model is None):
                return writer_rows[0]
        app_row = identity._replace(provider_device_id=None)
        if identity.provider_device_id and identity.source_app_id and (found := cls._find(rows, app_row)):
            return found
        if identity.keyed:
            return cls._find(rows, identity.legacy())
        return None

    @staticmethod
    def _learn_model(row: DataSource, device_model: str | None) -> bool:
        """Give a keyed row first seen without a model the provider's model. True if it did.

        Polar sleep arrives before the exercise that names the watch; a writer's first
        records may carry no model. Only for keyed rows, and only from a model the
        provider sent: a label describes the account, not this unit.
        """
        if row.device_model is not None or device_model is None:
            return False
        if not (row.provider_device_id or row.source_app_id):
            return False
        object.__setattr__(row, "device_model", device_model)
        object.__setattr__(row, "device_model_origin", DeviceModelOrigin.PROVIDER.value)
        return True

    @staticmethod
    def _follow_writer_name(row: DataSource, identity: DataSourceIdentity) -> bool:
        """An app-keyed row takes its writer's latest name (a renamed Apple Watch). True if it did.

        Upstream #1873. The name is not part of an app-keyed row's identity. Not on device-id
        rows - Samsung names one device differently per data type - and the app id itself
        is only a fallback name, so it never replaces a real one.
        """
        if not row.source_app_id or row.provider_device_id:
            return False
        source = identity.source
        if not source or source == row.source_app_id or source == row.source:
            return False
        object.__setattr__(row, "source", source)
        return True

    @staticmethod
    def _stamp_ids(row: DataSource, identity: DataSourceIdentity, device_manufacturer: str | None) -> bool:
        """Fill the stable ids (and manufacturer) a matched row lacks. True if anything changed.

        Ids are only ever added, never replaced: a row keyed by one unit's id must not be
        re-keyed to another's.
        """
        changed = False
        if identity.provider_device_id and row.provider_device_id is None:
            object.__setattr__(row, "provider_device_id", identity.provider_device_id)
            changed = True
        if identity.source_app_id and row.source_app_id is None:
            object.__setattr__(row, "source_app_id", identity.source_app_id)
            changed = True
        if device_manufacturer and row.device_manufacturer is None:
            object.__setattr__(row, "device_manufacturer", device_manufacturer)
            changed = True
        return changed

    def _connection_device_label(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        user_connection_id: UUID | None = None,
        at: datetime | None = None,
    ) -> str | None:
        """The manually-set or auto-derived device label behind a connection.

        Used to fill device_model when the provider reports none. Scoped to one
        connection when the caller knows which account it is ingesting for -
        without that, a user with two Whoops would stamp both units with
        whichever label happened to come back first.

        An account with a dated device history answers from it, for ``at``: the
        label stated for that instant, None for an instant no period covers, and the
        current period when ``at`` is None (app/utils/device_timeline.py).
        """
        timeline = load_timeline(db_session, user_connection_id)
        if timeline is not None:
            return timeline.label_at(at)
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
            {self.model.device_model: device_label, self.model.device_model_origin: DeviceModelOrigin.LABEL.value},
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
        recorded_at: datetime | None = None,
        reported_type: DeviceType | None = None,
        provider_device_id: str | None = None,
        source_app_id: str | None = None,
        device_manufacturer: str | None = None,
    ) -> DataSource:
        """The data source for one record, created if missing.

        ``recorded_at`` is when the record itself happened. It only matters when the
        provider named no device and the account has a dated device history: the
        label is then the one stated for that instant, not for today, so a late sync
        of an old night files under the device worn that night.
        """
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
        device_model_origin = DeviceModelOrigin.PROVIDER if device_model is not None else None
        if device_model is None:
            device_model = self._connection_device_label(
                db_session, user_id, provider, user_connection_id, at=recorded_at
            )
            if device_model is not None:
                device_model_origin = DeviceModelOrigin.LABEL

        # A sensor the account declared. It never replaces device_model - the
        # provider's report is kept verbatim, and the registry is where recorder and
        # sensor are separated - but it does decide what kind of device this source
        # is, because device_type is what ranks sources against each other. Left to
        # the reported model, an ECG strap recorded through a watch is classified as a
        # watch, and the reference instrument ends up ranked below the wrist sensors
        # it exists to be compared against.
        declared_sensor = self._connection_sensor_label(db_session, user_id, provider, user_connection_id)

        original_source_name = self._resolve_original_source_name(provider, device_model, source, original_source_name)

        identity = DataSourceIdentity(user_id, device_model, source, provider_device_id, source_app_id).with_clean_ids()
        existing = self._match(
            self._account_rows(db_session, provider, {user_id}, user_connection_id),
            identity,
        )
        if existing is None and user_connection_id is not None:
            # Adopt a source ingested before connections were recorded against
            # them (an XML import, or any row predating this column) rather than
            # creating a duplicate beside it. Only ever done when a single account
            # was alive when that row was written: with two, there is no way to
            # tell which of them the orphaned rows came from, and guessing would
            # file one unit's history under the other.
            existing = self._adoptable_orphan(db_session, user_id, provider, device_model, source, user_connection_id)

        if existing:
            updated = self._stamp_ids(existing, identity, device_manufacturer)
            # Stamped first, so a legacy row that just gained its ids can learn a model too.
            if device_model_origin is DeviceModelOrigin.PROVIDER:
                updated |= self._learn_model(existing, device_model)
            if user_connection_id and existing.user_connection_id is None:
                object.__setattr__(existing, "user_connection_id", user_connection_id)
                updated = True
            if software_version and _is_newer_version(software_version, existing.software_version):
                object.__setattr__(existing, "software_version", software_version)
                updated = True
            updated |= self._follow_writer_name(existing, identity)
            if original_source_name and existing.original_source_name is None:
                object.__setattr__(existing, "original_source_name", original_source_name)
                updated = True
            resolved = self._infer_device_type(
                device_model,
                original_source_name or existing.original_source_name,
                source,
                declared_sensor,
                provider=provider,
                reported_type=reported_type,
            )
            # Upstream #1902: a third-party writer's row keyed by the iPhone/Watch that
            # relayed it got its stored type from that host, so it is re-resolved outright
            # rather than left to the SDK upgrade-only rule, which would keep an Oura row
            # typed "phone" forever. A resolution of UNKNOWN still never erases a type.
            if is_host_model(existing.source_app_id, existing.device_model) and resolved is not DeviceType.UNKNOWN:
                device_type = resolved.value
            else:
                device_type = self.next_device_type(provider, existing.device_type, resolved)
            if device_type != existing.device_type:
                object.__setattr__(existing, "device_type", device_type)
                updated = True
            if updated:
                db_session.flush()
            self._attribute_device(db_session, existing, identity_claims)
            return existing

        provider_priority_repo = ProviderPriorityRepository(ProviderPriority)
        provider_priority_repo.ensure_provider_exists(db_session, provider)

        device_type = self._infer_device_type(
            device_model, original_source_name, source, declared_sensor, provider=provider, reported_type=reported_type
        )

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
            device_model_origin=device_model_origin.value if device_model_origin else None,
            provider_device_id=identity.provider_device_id,
            source_app_id=identity.source_app_id,
            device_manufacturer=device_manufacturer,
        )
        result = self.create(db_session, create_payload)
        assert result is not None
        self._attribute_device(db_session, result, identity_claims)
        return result

    def find_label_source(self, db_session: DbSession, template: DataSource, label: str) -> DataSource | None:
        """The source on ``template``'s account and writer whose device is ``label``, if any."""
        return (
            db_session.query(self.model)
            .filter(
                self._build_identity_filter(
                    template.user_id,
                    ProviderName(template.provider),
                    label,
                    template.source,
                    template.user_connection_id,
                )
            )
            .one_or_none()
        )

    def ensure_label_source(self, db_session: DbSession, template: DataSource, label: str) -> DataSource:
        """The label-derived sibling of ``template`` on its account, created if missing.

        For a re-file by dated device history: same user, provider, account and writer
        as the source rows are leaving, with ``label`` as the device. Flushed, never
        committed - a re-file is one transaction, and a destination committed on its
        own would survive a re-file that failed after creating it.

        The firmware version is not carried over: it described the device the rows
        are leaving.
        """
        existing = self.find_label_source(db_session, template, label)
        if existing is not None:
            return existing
        provider = ProviderName(template.provider)
        ProviderPriorityRepository(ProviderPriority).ensure_provider_exists(db_session, provider)
        declared_sensor = self._connection_sensor_label(
            db_session, template.user_id, provider, template.user_connection_id
        )
        original_source_name = self._resolve_original_source_name(
            provider, label, template.source, template.original_source_name
        )
        created = self.model(
            id=uuid4(),
            user_id=template.user_id,
            provider=provider,
            user_connection_id=template.user_connection_id,
            device_model=label,
            source=template.source,
            device_type=self._infer_device_type(label, original_source_name, template.source, declared_sensor).value,
            original_source_name=original_source_name,
            device_model_origin=DeviceModelOrigin.LABEL.value,
        )
        db_session.add(created)
        db_session.flush()
        self._attribute_device(db_session, created)
        return created

    def _provider_connections(
        self, db_session: DbSession, user_id: UUID, provider: ProviderName
    ) -> list[tuple[UUID, datetime]]:
        """This user's accounts with one provider, oldest first, as (id, created_at)."""
        rows = (
            db_session.query(UserConnection.id, UserConnection.created_at)
            .filter(UserConnection.user_id == user_id, UserConnection.provider == provider.value)
            .order_by(UserConnection.created_at.asc())
            .all()
        )
        return [(row[0], row[1]) for row in rows]

    def _can_adopt_orphan(
        self,
        connections: list[tuple[UUID, datetime]],
        user_connection_id: UUID,
        orphan_created_at: datetime | None,
    ) -> bool:
        """Whether this account can safely claim one connection-less row.

        The rule is still "only when exactly one account could have produced it", but
        asked *of the row* rather than of the user as they stand today. A row written
        while only one account existed can only have come from that account, however
        many have been linked since; the previous form asked how many accounts exist
        now, so linking a second one retroactively made every older row unclaimable
        and forked each device into a connection-less row plus a new one. Nothing
        upstream re-files the old row, so the split is permanent and both copies keep
        being written - which is what a validation study reads as a phantom doubled
        series.

        The narrowing is deliberate: adoption still refuses whenever two accounts were
        both alive when the row was written, because a wrong adoption merges two units'
        histories and cannot be undone, while a wrong split can.

        A row older than every connection (a one-time XML import that predates linking
        the account) falls back to the whole set, preserving the original behaviour for
        the import-then-connect flow.

        No connections read at all means the account being ingested for is not visible
        yet - it is being created in this same uncommitted transaction, which is exactly
        the connect-then-backfill path - and it is therefore the only one that can claim
        the row.
        """
        if not connections:
            return True
        candidates = connections
        if orphan_created_at is not None:
            earlier = [conn for conn in connections if conn[1] <= orphan_created_at]
            if earlier:
                candidates = earlier
        return len(candidates) == 1 and candidates[0][0] == user_connection_id

    def _adoptable_orphan(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: ProviderName,
        device_model: str | None,
        source: str | None,
        user_connection_id: UUID | None = None,
    ) -> DataSource | None:
        """A matching connection-less data source, when adopting it is unambiguous."""
        if user_connection_id is None:
            return None
        orphan = (
            db_session.query(self.model)
            .filter(self._build_identity_filter(user_id, provider, device_model, source, None))
            .one_or_none()
        )
        if orphan is None:
            return None
        connections = self._provider_connections(db_session, user_id, provider)
        if not self._can_adopt_orphan(connections, user_connection_id, orphan.created_at):
            return None
        return orphan

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
        provider: ProviderName | None = None,
        reported_type: DeviceType | None = None,
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

        After the declared sensor, two upstream signals (#1721): a provider that only
        ships one form factor (Oura, Ultrahuman, WHOOP) names its type outright, and a
        type the platform reported for itself is reconciled against the inference -
        see ``reconcile_device_type`` for when each one wins. Callers pass
        ``reported_type`` only from routes that report a real classification; the
        Apple route's deviceType is the iOS SDK's own guess and is filtered out by
        ``services/sdk/device_resolution`` before it gets here.
        """
        if declared_sensor:
            from_sensor = infer_device_type_from_source_name(declared_sensor)
            if from_sensor is not DeviceType.UNKNOWN:
                return from_sensor

        if provider in SINGLE_DEVICE_PROVIDER_TYPE:
            return SINGLE_DEVICE_PROVIDER_TYPE[provider]

        inferred = self._infer_from_strings(device_model, original_source_name, source)
        return reconcile_device_type(reported_type, inferred)

    def _infer_from_strings(
        self,
        device_model: str | None,
        original_source_name: str | None,
        source: str | None,
    ) -> DeviceType:
        """What the model and source strings alone say about a data source."""
        from_model = infer_device_type_from_model(device_model)

        if from_model in HANDSET_DEVICE_TYPES:
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

    def for_connection(self, db_session: DbSession, user_connection_id: UUID) -> list[DataSource]:
        """Every data source that arrived through one account."""
        return (
            db_session.query(self.model)
            .filter(self.model.user_connection_id == user_connection_id)
            .order_by(self.model.id)
            .all()
        )

    def retype_for_declaration(
        self,
        db_session: DbSession,
        data_source: DataSource,
        previous_sensor: str | None,
        declared_sensor: str | None,
    ) -> bool:
        """Re-classify a source after its account's declared sensor changed. True if it did.

        A sync applies a declaration only to the rows it touches, and on the SDK routes
        only as an upgrade, so changing the declaration would otherwise leave every
        existing row typed by the old one until it happened to sync again - the
        reference strap still ranked as a watch, or a withdrawn strap still ranked as
        one. A recognisable declaration wins outright, as it does at ingest. When it is
        withdrawn, a row whose type came from it is inferred afresh; any other row keeps
        what it has unless inference now says something better.
        """
        provider = ProviderName(getattr(data_source.provider, "value", data_source.provider))
        resolved = self._infer_device_type(
            data_source.device_model,
            data_source.original_source_name,
            data_source.source,
            declared_sensor,
            provider=provider,
        )
        declared_type = infer_device_type_from_source_name(declared_sensor) if declared_sensor else None
        previous_type = infer_device_type_from_source_name(previous_sensor) if previous_sensor else None
        if declared_type is not None and declared_type is not DeviceType.UNKNOWN:
            new_type = declared_type.value
        elif (
            previous_type is not None
            and previous_type is not DeviceType.UNKNOWN
            and data_source.device_type == previous_type.value
        ):
            new_type = resolved.value
        else:
            new_type = self.next_device_type(provider, data_source.device_type, resolved)
        if new_type == data_source.device_type:
            return False
        object.__setattr__(data_source, "device_type", new_type)
        db_session.flush()
        return True

    @staticmethod
    def next_device_type(provider: ProviderName, current: str | None, resolved: DeviceType) -> str:
        """The type an existing row should carry after a sync resolved ``resolved``.

        Cloud rows take the inferred type (upstream #1721), so a better mapping reaches
        rows that already exist; SDK rows only upgrade from unset, unknown or "other",
        because the SDK path's own reported-type upgrade (services/devices/detection)
        owns them. Two fork rules on top: a row always stores a value, "unknown"
        included, because consumers key off device_type to tell wearables from relays
        and NULL sends them back to guessing; and a sync that resolves UNKNOWN never
        erases a type already known - it carried no signal, it did not contradict one.
        """
        unset = (None, DeviceType.UNKNOWN.value)
        if resolved is DeviceType.UNKNOWN:
            return current if current not in unset else DeviceType.UNKNOWN.value
        if provider.value not in SDK_PROVIDERS:
            return resolved.value
        if current in (*unset, DeviceType.OTHER.value):
            return resolved.value
        return current

    def batch_ensure_data_sources(
        self,
        db_session: DbSession,
        provider: ProviderName,
        user_connection_id: UUID | None,
        identities: Iterable[tuple],
        reported_types: Mapping[Any, DeviceType] | None = None,
        *,
        label_identities: frozenset[tuple] = frozenset(),
        labels_resolved: bool = False,
        manufacturers: Mapping[Any, str] | None = None,
    ) -> dict[tuple, UUID]:
        """Data source ids for a batch of identities on one account, creating the missing.

        An identity is a DataSourceIdentity, or a plain (user_id, device_model, source)
        tuple for a route without stable ids; the result is keyed by what was passed.
        Matching follows _match: upstream #1781's tiers, within this account only.

        ``labels_resolved`` says the caller already applied the account's dated device
        history per record (resolve_bulk_data_sources), so a device_model still None
        here is a stretch no period covers and must stay None - falling back to the
        undated label would file it under today's device. ``label_identities`` are the
        identities whose device_model came from that history rather than the payload,
        so what is created from them is recorded as label-derived.
        """
        requested_list = list(dict.fromkeys(identities))
        if not requested_list:
            return {}

        # Same fallback as ensure_data_source: the bulk path is fed by provider
        # code that mostly does not carry a connection id of its own.
        if user_connection_id is None:
            user_connection_id = get_active_connection_id()

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
            if device_model is not None or labels_resolved:
                return device_model
            if user_id not in label_cache:
                label_cache[user_id] = self._connection_device_label(db_session, user_id, provider, user_connection_id)
            return label_cache[user_id]

        # Callers look rows up by the identity they passed in, which may carry a null
        # device_model, so keep both: what was asked for, and what is actually stored.
        stored_by_requested: dict[tuple, DataSourceIdentity] = {}
        for requested in requested_list:
            identity = DataSourceIdentity(*requested).with_clean_ids()
            stored_by_requested[requested] = identity._replace(
                device_model=_stored_device_model(identity.user_id, identity.device_model)
            )
        user_ids = {stored.user_id for stored in stored_by_requested.values()}
        label_requested = {DataSourceIdentity(*r).with_clean_ids() for r in label_identities}

        def _provider_model(requested: tuple) -> str | None:
            """The model the payload itself named for this identity, if any."""
            if requested[1] is None or DataSourceIdentity(*requested).with_clean_ids() in label_requested:
                return None
            return requested[1]

        reported_types = reported_types or {}
        manufacturers = manufacturers or {}

        # Every identity in one call shares the same connection, so matching runs over
        # this account's rows only and two accounts with one provider can no longer
        # collide on an identical (device_model, source) - or an identical device id.
        rows = self._account_rows(db_session, provider, user_ids, user_connection_id)
        matched: dict[tuple, DataSource] = {}
        changed = False
        for requested, stored in stored_by_requested.items():
            row = self._match(rows, stored)
            if row is not None:
                matched[requested] = row
                changed |= self._stamp_ids(row, stored, manufacturers.get(requested))
                changed |= self._learn_model(row, _provider_model(requested))
                changed |= self._follow_writer_name(row, stored)

        # Re-resolve the type of what already exists, as the single path does. The
        # caller's reported types are keyed on what it asked for; the first one per
        # row wins, as in upstream's resolver.
        reported_by_row: dict[UUID, DeviceType] = {}
        for requested, row in matched.items():
            if requested in reported_types:
                reported_by_row.setdefault(row.id, reported_types[requested])
        for row in {r.id: r for r in matched.values()}.values():
            device_type = self.next_device_type(
                provider,
                row.device_type,
                self._infer_device_type(
                    row.device_model,
                    row.original_source_name,
                    row.source,
                    _declared_sensor(row.user_id),
                    provider=provider,
                    reported_type=reported_by_row.get(row.id),
                ),
            )
            if device_type != row.device_type:
                object.__setattr__(row, "device_type", device_type)
                changed = True
        if changed:
            db_session.flush()

        # Adopt connection-less rows the same way ensure_data_source does, and
        # under the same condition: only when a single account was alive when the
        # row was written, so a row whose origin is unknowable is never guessed
        # onto one of two units. Without this the events path (which adopts) and
        # this time-series path (which would not) would file the same device under
        # two data sources - the exact split this method's docstring exists to
        # prevent.
        if user_connection_id is not None and len(matched) < len(stored_by_requested):
            orphan_rows = self._account_rows(db_session, provider, user_ids, None)
            # Read once: a payload can carry dozens of identities and the accounts
            # are the same for every one of them.
            connections_by_user: dict[UUID, list[tuple[UUID, datetime]]] = {}

            def _connections(user_id: UUID) -> list[tuple[UUID, datetime]]:
                if user_id not in connections_by_user:
                    connections_by_user[user_id] = self._provider_connections(db_session, user_id, provider)
                return connections_by_user[user_id]

            adopted = False
            for requested, stored in stored_by_requested.items():
                if requested in matched:
                    continue
                # Rows this batch already adopted count first, so two identities of one
                # orphan converge on it.
                row = self._match(rows, stored) or self._match(orphan_rows, stored)
                if row is None:
                    continue
                # Filtered per row, not per user: which accounts were alive depends on
                # when each row was written, so two orphans of one user can differ.
                if row.user_connection_id is None:
                    if not self._can_adopt_orphan(_connections(row.user_id), user_connection_id, row.created_at):
                        continue
                    object.__setattr__(row, "user_connection_id", user_connection_id)
                    orphan_rows.remove(row)
                    rows.append(row)
                self._stamp_ids(row, stored, manufacturers.get(requested))
                matched[requested] = row
                adopted = True
            if adopted:
                db_session.flush()

        missing = {requested: stored for requested, stored in stored_by_requested.items() if requested not in matched}
        if missing:
            # Who named each stored model, for what gets created below. A model the payload
            # carried is the provider's even if a label names the same string elsewhere in
            # the batch - the two share a row, and a capture must never become movable.
            provider_stored = {
                stored
                for requested, stored in stored_by_requested.items()
                if requested[1] is not None and DataSourceIdentity(*requested).with_clean_ids() not in label_requested
            }
            label_stored = {
                stored
                for requested, stored in stored_by_requested.items()
                if stored.device_model is not None
                and (requested[1] is None or DataSourceIdentity(*requested).with_clean_ids() in label_requested)
            }

            def _origin(stored: DataSourceIdentity) -> str | None:
                if stored in provider_stored:
                    return DeviceModelOrigin.PROVIDER.value
                if stored in label_stored:
                    return DeviceModelOrigin.LABEL.value
                return None

            # Keyed identities first, so an identity without ids can join a keyed row
            # created earlier in the same batch (upstream #1781's ordering).
            pending: list[DataSource] = []
            pending_for: dict[tuple, DataSource] = {}
            for requested, stored in sorted(missing.items(), key=lambda item: not item[1].keyed):
                row = self._match(pending, stored)
                if row is not None and self._learn_model(row, _provider_model(requested)):
                    object.__setattr__(
                        row,
                        "device_type",
                        self._infer_device_type(
                            row.device_model,
                            row.original_source_name,
                            row.source,
                            _declared_sensor(row.user_id),
                            provider=provider,
                            reported_type=reported_types.get(requested),
                        ).value,
                    )
                if row is None:
                    # Mirror ensure_data_source: derive the canonical brand here too, so rows
                    # first created by the bulk path aren't left with a NULL brand, and always
                    # store a device_type rather than NULL. Same rule as the single path,
                    # including keeping a readable writer name the brand tables do not know -
                    # otherwise one Muse source would read "Muse" and the other "Apple"
                    # depending only on which path happened to create the row first.
                    original_source_name = self._resolve_original_source_name(
                        provider, stored.device_model, stored.source, None
                    )
                    device_type = self._infer_device_type(
                        stored.device_model,
                        original_source_name,
                        stored.source,
                        _declared_sensor(stored.user_id),
                        provider=provider,
                        reported_type=reported_types.get(requested),
                    )
                    # Transient: only matched against within this batch, then inserted below.
                    row = self.model(
                        id=uuid4(),
                        user_id=stored.user_id,
                        provider=provider,
                        user_connection_id=user_connection_id,
                        device_model=stored.device_model,
                        source=stored.source,
                        device_type=device_type.value,
                        original_source_name=original_source_name,
                        device_model_origin=_origin(stored),
                        provider_device_id=stored.provider_device_id,
                        source_app_id=stored.source_app_id,
                        device_manufacturer=manufacturers.get(requested),
                    )
                    pending.append(row)
                pending_for[requested] = row

            values = [{field: getattr(row, field) for field in _INSERT_FIELDS} for row in pending]
            db_session.execute(insert(self.model).values(values).on_conflict_do_nothing())
            db_session.flush()

            # Re-read, so a row a concurrent sync inserted first is returned instead of
            # the transient one.
            stored_rows = self._account_rows(db_session, provider, user_ids, user_connection_id)
            created: dict[UUID, DataSource] = {}
            for requested, transient in pending_for.items():
                row = self._find(stored_rows, self._identity_of(transient))
                if row is not None:
                    matched[requested] = row
                    created.setdefault(row.id, row)

            # Attribute what this path created, mirroring ensure_data_source. Time
            # series arrive through here while events arrive through the single path,
            # so skipping it would leave one device's streams half attributed. The
            # loop is over distinct identities, not samples, so it stays small.
            for data_source in created.values():
                self._attribute_device(db_session, data_source)

        return {requested: row.id for requested, row in matched.items()}

    @staticmethod
    def _identity_of(row: DataSource) -> DataSourceIdentity:
        return DataSourceIdentity(row.user_id, row.device_model, row.source, row.provider_device_id, row.source_app_id)

    def resolve_bulk_data_sources(
        self,
        db_session: DbSession,
        creators: Sequence[_Creator],
        recorded_at: Callable[[_Creator], datetime | None],
    ) -> list[UUID | None]:
        """The data source id for each creator, in order; None where none resolved.

        The one resolver both bulk writers (events, time series) use, so the two
        cannot drift apart - a single device split across two sources is what the
        drift looked like.

        Grouped by provider and account, as each writer did before. For an account
        with a dated device history, a creator whose payload named no device takes
        the label stated for ``recorded_at(creator)``, so one batch spanning a switch
        lands on two sources. The result is per creator rather than per identity:
        two accounts in one batch can share an identical (user, model, source), and
        a map keyed on that alone would file the first account's rows under the
        second.
        """
        result: list[UUID | None] = [None] * len(creators)
        groups: dict[tuple[ProviderName, UUID | None], list[int]] = {}
        for i, creator in enumerate(creators):
            provider = self.infer_provider_from_source(creator.source)
            if creator.provider:
                with contextlib.suppress(ValueError):
                    provider = ProviderName(creator.provider)
            groups.setdefault((provider, creator.user_connection_id), []).append(i)

        for (provider, user_connection_id), indexes in groups.items():
            # The account batch_ensure_data_sources will write under, found the same way.
            account_id = user_connection_id if user_connection_id is not None else get_active_connection_id()
            timeline = load_timeline(db_session, account_id)

            keys: dict[int, DataSourceIdentity] = {}
            label_identities: set[DataSourceIdentity] = set()
            provider_identities: set[DataSourceIdentity] = set()
            # The platform's own classification, where the route sent one (upstream
            # #1721). First one wins per identity, as in upstream's resolver.
            reported_types: dict[DataSourceIdentity, DeviceType] = {}
            manufacturers: dict[DataSourceIdentity, str] = {}
            for i in indexes:
                creator = creators[i]
                model = creator.device_model
                if model is None and timeline is not None:
                    model = timeline.label_at(recorded_at(creator))
                    if model is not None:
                        label_identities.add(creator_identity(creator, model))
                elif model is not None:
                    provider_identities.add(creator_identity(creator, model))
                keys[i] = creator_identity(creator, model)
                reported = getattr(creator, "device_type", None)
                if reported is not None:
                    reported_types.setdefault(keys[i], reported)
                manufacturer = getattr(creator, "device_manufacturer", None)
                if manufacturer:
                    manufacturers.setdefault(keys[i], manufacturer)

            ids = self.batch_ensure_data_sources(
                db_session,
                provider,
                user_connection_id,
                set(keys.values()),
                label_identities=frozenset(label_identities - provider_identities),
                labels_resolved=timeline is not None,
                reported_types=reported_types,
                manufacturers=manufacturers,
            )
            for i, key in keys.items():
                result[i] = ids.get(key)
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

        Phones are left out. A phone's model on an Apple or Health Connect account
        is the handset that ran an app, not a device anyone wore, and listing it
        beside the watches only invites mapping the wrong thing.
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
            if connection_id is None or device_model is None:  # excluded by the query; narrows the types
                continue
            if is_handset_model(device_model):
                continue
            name = humanize_device_model(device_model) or device_model
            seen = observed.setdefault(connection_id, [])
            if name not in seen:
                seen.append(name)
        return observed

    def get_writer_siblings(self, db_session: DbSession, data_source_ids: set[UUID]) -> dict[UUID, tuple[UUID, ...]]:
        """Other sources of the same writer on the same account and device, for each given source.

        Upstream #1866: one writer can be split across sources by its device model (Health
        Connect apps reporting the model on some records only, Garmin dailies vs
        workouts), so a workout's heart rate may sit on a sibling source. Upstream groups
        by (user, provider, app id else source) alone. Here a sibling must also be on the
        same account, must not be attributed to a different registry device, and must not
        report a different model: a participant's second Garmin, or a bike computer beside
        the watch, writes as "garmin" too, and its heart rate is another unit's reading.
        """
        if not data_source_ids:
            return {}
        owners = select(self.model.user_id).where(self.model.id.in_(data_source_ids))
        rows = db_session.query(self.model).filter(self.model.user_id.in_(owners)).all()
        by_writer: dict[tuple[UUID, str, UUID | None, str], list[DataSource]] = defaultdict(list)
        for row in rows:
            name = row.source_app_id or row.source
            if name:
                by_writer[(row.user_id, str(row.provider), row.user_connection_id, name)].append(row)

        def _same_unit(a: DataSource, b: DataSource) -> bool:
            if a.device_id is not None and b.device_id is not None and a.device_id != b.device_id:
                return False
            return a.device_model is None or b.device_model is None or a.device_model == b.device_model

        result: dict[UUID, tuple[UUID, ...]] = {}
        for row in rows:
            if row.id not in data_source_ids:
                continue
            name = row.source_app_id or row.source
            # A source with neither an app id nor a source name has no writer to share
            group = by_writer.get((row.user_id, str(row.provider), row.user_connection_id, name), []) if name else []
            result[row.id] = tuple(other.id for other in group if other.id != row.id and _same_unit(row, other))
        return result

    def get_user_data_sources(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> list[DataSource]:
        return (
            db_session.query(self.model)
            .filter(self.model.user_id == user_id)
            .order_by(self.model.provider.asc(), self.model.device_model.asc())
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
        data_type_coverage_repository.delete_for_provider(db_session, user_id, provider)
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
        # Coverage (upstream #1597) is kept per (user, provider), not per account. Cleared
        # when this was the provider's last account with data; with another account still
        # holding data the stored range may overstate until that account's next sync.
        provider = db_session.query(UserConnection.provider).filter(UserConnection.id == user_connection_id).scalar()
        if provider is not None and not (
            db_session.query(self.model.id)
            .filter(self.model.user_id == user_id, self.model.provider == provider)
            .first()
        ):
            data_type_coverage_repository.delete_for_provider(db_session, user_id, provider)
        db_session.commit()
        return result.rowcount

    def infer_provider_from_source(self, source: str | None) -> ProviderName:
        """Infer provider from source string.

        Deprecated: Use ProviderName.from_source_string() directly instead.
        This method is kept for backward compatibility.
        """
        return ProviderName.from_source_string(source)
