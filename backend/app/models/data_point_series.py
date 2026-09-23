from uuid import UUID
from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import (
    FKDataSource,
    FKSeriesTypeDefinition,
    PrimaryKey,
    json_object,
    numeric_10_3,
    str_10,
    str_100,
)


class DataPointSeries(BaseDbModel):
    """Unified time-series data points for device metrics (heart rate, steps, energy, etc.)."""

    __tablename__ = "data_point_series"
    __table_args__ = (
        UniqueConstraint(
            "data_source_id",
            "series_type_definition_id",
            "recorded_at",
            name="uq_data_point_series_source_type_time",
        ),
    )

    id: Mapped[PrimaryKey[UUID]]
    external_id: Mapped[str_100 | None]
    data_source_id: Mapped[FKDataSource]
    recorded_at: Mapped[datetime]
    zone_offset: Mapped[str_10 | None]
    value: Mapped[numeric_10_3]
    series_type_definition_id: Mapped[FKSeriesTypeDefinition]
    is_daily_total: Mapped[bool | None] # True = pre-aggregated daily total; False = granular intraday samples
    # Per-sample metadata exactly as the platform sent it, or NULL when it sent none.
    #
    # On HealthKit this is where a heart-rate sample's provenance lives: the Bluetooth
    # peripheral that produced it and where on the body it was measured. Nothing else on
    # the row distinguishes a chest strap from a wrist sensor when both arrive through
    # one writing app on one phone.
    #
    # Raw and uninterpreted on purpose. Keys differ per platform and per writer, and the
    # iOS SDK stringifies values on the way out ("70 count/min"), so anything typed has
    # to be derived from this rather than replace it.
    provider_metadata: Mapped[json_object | None]
