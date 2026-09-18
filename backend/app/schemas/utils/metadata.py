from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from app.constants.devices_map import resolve_device_name
from app.schemas.enums import DeviceType, Resolution


class SourceMetadata(BaseModel):
    """Attribution for a sample or record.

    ``provider`` is the integration the data arrived through (apple, garmin, ...).
    ``source`` is the writer inside that integration - a third-party app name for
    HealthKit/Health Connect data ("Connect", "Zepp Life"), or the provider key
    itself for native API integrations.
    """

    provider: str = Field(..., example="apple")
    source: str | None = Field(None, example="Connect")
    device: str | None = Field(None, example="iPhone15,2")
    device_type: DeviceType | None = Field(None, example="phone")

    @computed_field
    @property
    def device_name(self) -> str | None:
        """Marketing name for ``device``, derived so it cannot drift from the raw model."""
        return resolve_device_name(self.device)

    # Identity fields — let a consumer join a sample straight back to the row from
    # GET /users/{id}/data-sources instead of re-deriving a composite key.
    data_source_id: UUID | None = Field(
        None,
        description="Stable DataSource id. Identical to `items[].id` from /users/{user_id}/data-sources.",
    )
    ingestion_provider: str | None = Field(
        None,
        description=(
            "The ingestion path (DataSource.provider), e.g. apple, google, garmin. "
            "Always equal to `provider`; kept as a stable name for consumers that "
            "pinned to it while `provider` still carried the sub-source tag."
        ),
        example="apple",
    )
    source_tag: str | None = Field(
        None,
        description="Sub-source tag (DataSource.source), e.g. an Apple HealthKit bundle id. Equal to `source`.",
        example="com.oura.oura",
    )
    original_source_name: str | None = Field(
        None, description="Canonical brand the data came from (DataSource.original_source_name).", example="Oura"
    )
    ingestion_route: str | None = Field(
        None,
        description=(
            "`direct` if the data came straight from the maker's API, `aggregator` if it was "
            "relayed through a platform such as Apple Health or Google Health. When "
            "`aggregator`, `ingestion_provider` names the platform and `original_source_name` "
            "names the brand that recorded it."
        ),
        example="aggregator",
    )

    # The physical unit, as opposed to the ingest path above. Two samples sharing a
    # device_id came from one piece of hardware even when their provider, source and
    # ingestion_route all differ - which is the case for a ring read from its maker's
    # API and the same ring relayed through Apple Health. Group on device_id to pool a
    # device's data; keep data_source_id to tell the two routes apart, since the relay
    # changes freshness, rounding and completeness.
    device_id: UUID | None = Field(
        None,
        description=(
            "Stable id of the physical device, or null when the source is not attributed to "
            "one. Survives a model-string change, and is shared across ingest routes once the "
            "routes have been linked. Identical to `items[].id` from /users/{user_id}/devices."
        ),
    )
    device_label: str | None = Field(
        None,
        description="Human-assigned name for the device, if one has been set.",
        example="Sub 04 fenix",
    )
    # Which connected provider account this sample arrived through. A user may
    # hold several accounts with one provider, and then the provider alone no
    # longer says where a sample came from - this does. Resolve it against
    # GET /users/{user_id}/connections for the account's label and e-mail; the
    # id is on the data_source row, so exposing it costs no extra query on a
    # path that runs per sample.
    user_connection_id: UUID | None = Field(
        None,
        description=(
            "Id of the connected provider account this data came through, or null for "
            "one-time imports. Identical to `id` from /users/{user_id}/connections."
        ),
    )

    @classmethod
    def from_data_source(cls, data_source: Any) -> "SourceMetadata":
        """Build attribution from a DataSource row, including the join identity."""
        # Imported here rather than at module scope: app.utils.device_registry imports
        # from app.schemas.enums, so a top-level import would close an import cycle.
        from app.utils.device_registry import resolve_ingestion_route

        # .value, not str(): ProviderName is a (str, Enum), so str(member) renders as
        # "ProviderName.GARMIN" rather than "garmin". A row read back from the database
        # arrives as a plain string and is unaffected, but one still holding the enum
        # (a freshly created DataSource) would put the member name into the response.
        provider = getattr(data_source.provider, "value", data_source.provider)

        return cls(
            provider=provider if provider else "unknown",
            source=data_source.source,
            device=data_source.device_model,
            data_source_id=data_source.id,
            ingestion_provider=provider if provider else None,
            source_tag=data_source.source,
            original_source_name=data_source.original_source_name,
            device_type=data_source.device_type,
            ingestion_route=(
                resolve_ingestion_route(data_source.provider, data_source.original_source_name).value
                if data_source.provider
                else None
            ),
            device_id=getattr(data_source, "device_id", None),
            user_connection_id=getattr(data_source, "user_connection_id", None),
            # Read through the relationship only when it is already loaded. This runs
            # per sample on hot read paths, and a lazy load here would turn one
            # timeseries response into a query per row.
            device_label=_loaded_device_label(data_source),
        )


class TimeseriesMetadata(BaseModel):
    resolution: Resolution | None = None
    sample_count: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None


def _loaded_device_label(data_source: Any) -> str | None:
    """The attributed device's label, but only if the relationship is already loaded.

    Returns None rather than emitting a query, so a caller that has not joined the
    device gets the id (which is on the row itself) and no label, instead of an N+1.
    """
    from sqlalchemy import inspect as sa_inspect

    if getattr(data_source, "device_id", None) is None:
        return None
    try:
        state = sa_inspect(data_source)
    except Exception:
        return None
    if "device" in getattr(state, "unloaded", ()):
        return None
    device = getattr(data_source, "device", None)
    return getattr(device, "label", None)
