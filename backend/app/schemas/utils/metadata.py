from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from app.constants.devices_map import resolve_device_name
from app.schemas.enums import DeviceType


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

    @classmethod
    def from_data_source(cls, data_source: Any) -> "SourceMetadata":
        """Build attribution from a DataSource row, including the join identity."""
        # Imported here rather than at module scope: app.utils.device_registry imports
        # from app.schemas.enums, so a top-level import would close an import cycle.
        from app.utils.device_registry import resolve_ingestion_route

        return cls(
            provider=str(data_source.provider) if data_source.provider else "unknown",
            source=data_source.source,
            device=data_source.device_model,
            data_source_id=data_source.id,
            ingestion_provider=str(data_source.provider) if data_source.provider else None,
            source_tag=data_source.source,
            original_source_name=data_source.original_source_name,
            device_type=data_source.device_type,
            ingestion_route=(
                resolve_ingestion_route(data_source.provider, data_source.original_source_name).value
                if data_source.provider
                else None
            ),
        )


class TimeseriesMetadata(BaseModel):
    resolution: Literal["raw", "1min", "5min", "15min", "1hour"] | None = None
    sample_count: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
