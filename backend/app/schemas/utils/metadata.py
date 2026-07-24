from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class SourceMetadata(BaseModel):
    """Attribution for a sample or record.

    `provider` and `device` are the original two fields and are unchanged. Note that
    on time-series samples `provider` historically carries the *sub-source tag*
    (e.g. an Apple HealthKit bundle id), not the ingestion provider — the explicit
    fields below disambiguate that without breaking existing consumers.
    """

    provider: str = Field(..., example="apple_health")
    device: str | None = Field(None, example="Apple Watch Series 9")

    # Identity fields — let a consumer join a sample straight back to the row from
    # GET /users/{id}/data-sources instead of re-deriving a composite key.
    data_source_id: UUID | None = Field(
        None,
        description="Stable DataSource id. Identical to `items[].id` from /users/{user_id}/data-sources.",
    )
    ingestion_provider: str | None = Field(
        None, description="The ingestion path (DataSource.provider), e.g. apple, google, garmin.", example="apple"
    )
    source_tag: str | None = Field(
        None,
        description="Sub-source tag (DataSource.source), e.g. an Apple HealthKit bundle id.",
        example="com.oura.oura",
    )
    original_source_name: str | None = Field(
        None, description="Canonical brand the data came from (DataSource.original_source_name).", example="Oura"
    )
    device_type: str | None = Field(
        None, description="chest_strap | watch | band | ring | phone | scale | other | unknown.", example="chest_strap"
    )

    @classmethod
    def from_data_source(cls, data_source: Any) -> "SourceMetadata":
        """Build attribution from a DataSource row, including the join identity.

        `provider` keeps its historical value (the sub-source tag, falling back to
        "unknown") so existing consumers and the frontend are unaffected.
        """
        return cls(
            provider=data_source.source or "unknown",
            device=data_source.device_model,
            data_source_id=data_source.id,
            ingestion_provider=str(data_source.provider) if data_source.provider else None,
            source_tag=data_source.source,
            original_source_name=data_source.original_source_name,
            device_type=data_source.device_type,
        )


class TimeseriesMetadata(BaseModel):
    resolution: Literal["raw", "1min", "5min", "15min", "1hour"] | None = None
    sample_count: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
