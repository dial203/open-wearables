"""What each of a user's data sources has actually reported lately.

The registry can say where a stream came from but not what is in it, and for an
unidentifiable source that is the only question that matters. "Bluetooth Device"
names nothing; fourteen nights of sleep and a recovery score every morning name a
ring or a band, and three cycling workouts name a head unit. This is the evidence
someone needs to decide what a device is and where it is worn.

Every query here is grouped over the user's whole set of sources in one statement
rather than run per source. A device listing walks every source of every device, and
a participant in a multi-device study has dozens - per-source queries would turn one
page into a hundred round trips against the largest tables in the schema.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select

from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecord, HealthScore, SeriesTypeDefinition


class SourceActivityRow:
    """One (source, kind, label) bucket: how much arrived and when it last did."""

    __slots__ = ("data_source_id", "label", "count", "last_at", "first_at")

    def __init__(self, data_source_id: UUID, label: str, count: int, last_at: datetime, first_at: datetime) -> None:
        self.data_source_id = data_source_id
        self.label = label
        self.count = count
        self.last_at = last_at
        self.first_at = first_at


class SourceActivityRepository:
    @staticmethod
    def _since(days: int) -> datetime:
        return datetime.now(UTC) - timedelta(days=days)

    def _source_ids(self, db_session: DbSession, user_id: UUID) -> list[UUID]:
        return list(db_session.scalars(select(DataSource.id).where(DataSource.user_id == user_id)).all())

    def events(self, db_session: DbSession, user_id: UUID, days: int) -> list[SourceActivityRow]:
        """Sleep sessions, workouts, naps - counted by the category the provider gave.

        Grouped on (data_source_id, category), which ix_event_record_source_category
        already covers, so this stays an index scan however much history is behind it.
        """
        stmt = (
            select(
                EventRecord.data_source_id,
                EventRecord.category,
                func.count().label("n"),
                func.max(EventRecord.start_datetime).label("last_at"),
                func.min(EventRecord.start_datetime).label("first_at"),
            )
            .join(DataSource, DataSource.id == EventRecord.data_source_id)
            .where(DataSource.user_id == user_id, EventRecord.start_datetime >= self._since(days))
            .group_by(EventRecord.data_source_id, EventRecord.category)
        )
        return [SourceActivityRow(r[0], r[1], r[2], r[3], r[4]) for r in db_session.execute(stmt).all()]

    def scores(self, db_session: DbSession, user_id: UUID, days: int) -> list[SourceActivityRow]:
        """Readiness, recovery, strain, sleep score - whatever the provider scores.

        Filtered on health_score.user_id directly rather than joined through the data
        source: the column is indexed and the join would buy nothing. Rows with no
        data_source_id are skipped - they belong to the user, not to a device, so they
        cannot help identify one.
        """
        stmt = (
            select(
                HealthScore.data_source_id,
                HealthScore.category,
                func.count().label("n"),
                func.max(HealthScore.recorded_at).label("last_at"),
                func.min(HealthScore.recorded_at).label("first_at"),
            )
            .where(
                HealthScore.user_id == user_id,
                HealthScore.data_source_id.isnot(None),
                HealthScore.recorded_at >= self._since(days),
            )
            .group_by(HealthScore.data_source_id, HealthScore.category)
        )
        return [SourceActivityRow(r[0], _value_of(r[1]), r[2], r[3], r[4]) for r in db_session.execute(stmt).all()]

    def metrics(self, db_session: DbSession, user_id: UUID, days: int) -> list[SourceActivityRow]:
        """Which time-series a source carries: heart rate, HRV, SpO2, steps.

        The metric names are what separate a chest strap from a ring even when both
        report sleep. Grouped on (data_source_id, series type), the leading columns of
        uq_data_point_series_source_type_time.
        """
        stmt = (
            select(
                DataPointSeries.data_source_id,
                SeriesTypeDefinition.code,
                func.count().label("n"),
                func.max(DataPointSeries.recorded_at).label("last_at"),
                func.min(DataPointSeries.recorded_at).label("first_at"),
            )
            .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
            .join(
                SeriesTypeDefinition,
                SeriesTypeDefinition.id == DataPointSeries.series_type_definition_id,
            )
            .where(DataSource.user_id == user_id, DataPointSeries.recorded_at >= self._since(days))
            .group_by(DataPointSeries.data_source_id, SeriesTypeDefinition.code)
        )
        return [SourceActivityRow(r[0], r[1], r[2], r[3], r[4]) for r in db_session.execute(stmt).all()]


def _value_of(category: object) -> str:
    """Enum columns come back as the enum on some paths and the raw string on others."""
    return str(getattr(category, "value", category))
