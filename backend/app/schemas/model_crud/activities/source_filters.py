from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.enums import ProviderName


class SourceFilterParams(BaseModel):
    """Filters narrowing a read to one data origin, shared by every query over normalized data."""

    provider: ProviderName | None = Field(None, description="Provider filter")
    source: str | None = Field(None, description="Data source filter")
    device_model: str | None = Field(None, description="Device model filter")
    data_source_id: UUID | None = Field(None, description="Direct data source identifier filter")
    include_redundant_relays: bool = Field(
        False,
        description=(
            "Keep the aggregator's copy of a maker that is also connected directly. By default a "
            "read leaves out relayed data (Garmin or Oura arriving through Apple Health, say) for "
            "the span in which the maker's own API delivered the same series type, so one device "
            "is not counted twice. Nothing is deleted: set this to `true` to read the relayed "
            "copies back - to check the direct route for completeness, or to compare the two paths."
        ),
    )
