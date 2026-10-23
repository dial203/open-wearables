from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import and_, asc, desc, func, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models import DataSource, HealthScore
from app.repositories.data_point_series_repository import WriteCounts
from app.repositories.repositories import CrudRepository
from app.schemas.enums import HealthScoreCategory
from app.schemas.model_crud.activities import HealthScoreCreate, HealthScoreQueryParams, HealthScoreUpdate

if TYPE_CHECKING:
    # Annotation only: the rule lives in the service layer and hands the plan down.
    from app.services.sources.relay_dedup import RelayDedupPlan
from app.utils.pagination import decode_cursor


def _first_component_value(components: dict[str, Any], *keys: str) -> Any:
    """Return the first present component value across candidate keys.

    Providers store the same recovery metric under different `components` keys:
    Oura/Garmin use 'resting_heart_rate' / 'hrv_rmssd_milli', while Polar's nightly
    recharge uses 'heart_rate_avg' (overnight average HR) / 'heart_rate_variability_avg'
    (RMSSD, ms). Coalescing across the aliases lets the recovery summary surface RHR/HRV
    from every provider instead of only the ones using the canonical keys.
    """
    for key in keys:
        entry = components.get(key)
        if isinstance(entry, dict) and entry.get("value") is not None:
            return entry["value"]
    return None


class HealthScoreRepository(CrudRepository[HealthScore, HealthScoreCreate, HealthScoreUpdate]):
    def get_by_all_components(self, db_session: DbSession, components: list[str]) -> list[HealthScore]:
        """Return health scores whose components JSONB contains all specified keys (?& operator)."""
        return db_session.query(HealthScore).filter(HealthScore.components.has_all(components)).all()

    def get_by_any_component(self, db_session: DbSession, components: list[str]) -> list[HealthScore]:
        """Return health scores whose components JSONB contains any of the specified keys (?| operator)."""
        return db_session.query(HealthScore).filter(HealthScore.components.has_any(components)).all()

    def get_with_filters(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: HealthScoreQueryParams,
    ) -> tuple[list[HealthScore], int]:
        filters = [HealthScore.user_id == user_id]

        if params.category:
            filters.append(HealthScore.category == params.category)
        if params.provider:
            filters.append(HealthScore.provider == params.provider)
        if params.data_source_id:
            filters.append(HealthScore.data_source_id == params.data_source_id)
        if params.start_datetime:
            filters.append(HealthScore.recorded_at >= params.start_datetime)
        if params.end_datetime:
            filters.append(HealthScore.recorded_at < params.end_datetime)

        query = db_session.query(HealthScore).filter(and_(*filters))

        total_count = query.count()
        results = query.order_by(HealthScore.recorded_at.desc()).offset(params.offset).limit(params.limit).all()
        return results, total_count

    def bulk_create(self, db_session: DbSession, creators: list[HealthScoreCreate]) -> WriteCounts:
        """Bulk insert health scores, doing nothing on conflict with the unique constraint.

        Returns the rows actually written. DO NOTHING never refreshes an existing
        row, so the count is all inserts — RETURNING yields a row only for the
        ones that were not conflicts.
        """
        if not creators:
            return WriteCounts(0, 0)

        values = [c.model_dump() for c in creators]

        stmt = insert(HealthScore).values(values).on_conflict_do_nothing().returning(HealthScore.id)
        inserted = len(db_session.execute(stmt).scalars().all())
        # Caller is responsible for commit — allows batching with other operations
        return WriteCounts(inserted, 0)

    def get_latest_by_category(
        self,
        db_session: DbSession,
        user_id: UUID,
        category: HealthScoreCategory,
    ) -> HealthScore | None:
        """Return the most recent health score for a given category and user."""
        return (
            db_session.query(HealthScore)
            .filter(HealthScore.user_id == user_id, HealthScore.category == category)
            .order_by(HealthScore.recorded_at.desc())
            .first()
        )

    def delete_for_user_date(
        self,
        db_session: DbSession,
        user_id: UUID,
        score_date: date,
        category: HealthScoreCategory,
        provider: str = "internal",
    ) -> int:
        """Delete health scores matching user/category/provider/date without loading objects.

        Caller is responsible for commit. Returns deleted row count.
        Matches the whole day rather than an exact timestamp: sleep scores are anchored to
        the session's local wake time, and older rows sit at midnight of the same date.
        """
        day_start = datetime(score_date.year, score_date.month, score_date.day, tzinfo=timezone.utc)
        return (
            db_session.query(HealthScore)
            .filter(
                HealthScore.user_id == user_id,
                HealthScore.provider == provider,
                HealthScore.category == category,
                HealthScore.recorded_at >= day_start,
                HealthScore.recorded_at < day_start + timedelta(days=1),
            )
            .delete(synchronize_session=False)
        )

    def get_recovery_summaries(
        self,
        db_session: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
        cursor: str | None,
        limit: int,
        relay_plan: "RelayDedupPlan | None" = None,
        page_by_date: bool = False,
    ) -> list[dict[str, Any]]:
        """Get recovery health scores for a date range with cursor-based pagination.

        ``page_by_date`` pages by day rather than by score row (upstream #1844's fix,
        without moving the source choice into SQL): every score of ``limit + 1`` UTC days,
        so a caller keeping one source per day gets a full page and never sees a day split
        across two pages. The fork's per-day choice stays in the summaries service.

        Returns list of dicts with keys: recovery_date, provider, source, device_model,
        device_type, device_id, data_source_id, user_connection_id, original_source_name,
        record_id, recorded_at, recovery_score, resting_heart_rate, hrv_rmssd_milli,
        spo2_percentage.
        Fetches limit+1 rows so callers can detect has_more without a separate COUNT query.
        Ordering matches get_sleep_summaries: ASC by default, DESC when paginating backward.
        """
        # Outer join so scores without a data_source (older rows) still come back.
        query = (
            db_session.query(HealthScore, DataSource)
            .outerjoin(DataSource, HealthScore.data_source_id == DataSource.id)
            .filter(
                HealthScore.user_id == user_id,
                HealthScore.category == HealthScoreCategory.RECOVERY,
                HealthScore.recorded_at >= start_date,
                HealthScore.recorded_at < end_date,
                # A score the aggregator relayed for a day the maker's own API already
                # scored is the same day twice. Scores with no data_source (older rows)
                # carry a NULL id, which no span matches, so they come through untouched.
                *(
                    relay_plan.conditions(
                        HealthScore.data_source_id,
                        key_column=HealthScore.category,
                        timestamp_column=HealthScore.recorded_at,
                    )
                    if relay_plan is not None
                    else []
                ),
            )
        )

        if page_by_date:
            # The day recovery_date reports: the UTC date of recorded_at.
            day = func.date(func.timezone("UTC", HealthScore.recorded_at))
            days = query.with_entities(day).distinct()
            descending = False
            if cursor:
                cursor_ts, _cursor_id, direction = decode_cursor(cursor)
                descending = direction == "prev"
                cursor_day = cursor_ts.astimezone(timezone.utc).date() if cursor_ts.tzinfo else cursor_ts.date()
                days = days.filter(day < cursor_day) if descending else days.filter(day > cursor_day)
            page = [r[0] for r in days.order_by(desc(day) if descending else asc(day)).limit(limit + 1).all()]
            if not page:
                return []
            ordering = (
                (HealthScore.recorded_at.desc(), HealthScore.id.desc())
                if descending
                else (HealthScore.recorded_at.asc(), HealthScore.id.asc())
            )
            query = query.filter(day.in_(page)).order_by(*ordering)
            rows = query.all()
        elif cursor:
            cursor_ts, cursor_id, direction = decode_cursor(cursor)
            if direction == "prev":
                query = query.filter(tuple_(HealthScore.recorded_at, HealthScore.id) < (cursor_ts, cursor_id)).order_by(
                    HealthScore.recorded_at.desc(), HealthScore.id.desc()
                )
            else:
                query = query.filter(tuple_(HealthScore.recorded_at, HealthScore.id) > (cursor_ts, cursor_id)).order_by(
                    HealthScore.recorded_at.asc(), HealthScore.id.asc()
                )
        else:
            query = query.order_by(HealthScore.recorded_at.asc(), HealthScore.id.asc())

        if not page_by_date:
            rows = query.limit(limit + 1).all()

        summaries = []
        for row, data_source in rows:
            components = cast(dict, row.components or {})
            summaries.append(
                {
                    "recovery_date": row.recorded_at.date(),
                    "provider": row.provider,
                    "source": data_source.source if data_source else None,
                    "device_model": data_source.device_model if data_source else None,
                    "device_type": data_source.device_type if data_source else None,
                    "device_id": data_source.device_id if data_source else None,
                    # One score is one data source, so unlike the grouped daily
                    # aggregates these rows can name theirs - which is what lets a
                    # viewer attribute an unidentified source straight from the row.
                    "data_source_id": data_source.id if data_source else None,
                    "user_connection_id": data_source.user_connection_id if data_source else None,
                    "original_source_name": data_source.original_source_name if data_source else None,
                    "record_id": row.id,
                    "recorded_at": row.recorded_at,
                    "recovery_score": int(row.value) if row.value is not None else None,
                    # Coalesce provider-specific aliases so Polar (heart_rate_avg /
                    # heart_rate_variability_avg) surfaces alongside Oura/Garmin.
                    "resting_heart_rate": _first_component_value(components, "resting_heart_rate", "heart_rate_avg"),
                    "hrv_rmssd_milli": _first_component_value(
                        components, "hrv_rmssd_milli", "heart_rate_variability_avg"
                    ),
                    "spo2_percentage": _first_component_value(components, "spo2_percentage"),
                }
            )
        return summaries

    def get_latest_per_category(
        self,
        db_session: DbSession,
        user_id: UUID,
    ) -> list[HealthScore]:
        """Return the most recent score for each category for a given user.

        Uses PostgreSQL DISTINCT ON (category) for efficiency.
        """
        return (
            db_session.query(HealthScore)
            .filter(HealthScore.user_id == user_id)
            .distinct(HealthScore.category)
            .order_by(HealthScore.category, HealthScore.recorded_at.desc())
            .all()
        )
