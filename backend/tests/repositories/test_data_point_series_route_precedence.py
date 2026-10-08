"""Two feeds of one provider that meet on the same second: the higher-ranked one keeps it.

A data source's series is keyed on (source, type, second), and before feeds were
recorded the last write owned the key. A Garmin run came back with an all-day
one-minute average on every :00/:15/:30/:45 second because of it. The route ranks the
feeds - workout > intraday > window > summary - so the order things arrive in no longer
decides what a second holds. A daily total against a sample keeps last-write-wins.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataPointSeries
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.schemas.enums import SampleRoute, SeriesType, get_series_type_id, route_id_for
from app.schemas.model_crud.activities import TimeSeriesQueryParams, TimeSeriesSampleCreate
from app.services.timeseries_service import timeseries_service
from tests.factories import UserFactory

T0 = datetime(2026, 10, 6, 2, 44, 15, tzinfo=timezone.utc)


def _sample(
    user_id: UUID,
    value: int,
    route: SampleRoute | None,
    *,
    at: datetime = T0,
    series_type: SeriesType = SeriesType.heart_rate,
    is_daily_total: bool | None = None,
) -> TimeSeriesSampleCreate:
    return TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=user_id,
        source="garmin",
        provider="garmin",
        recorded_at=at,
        value=value,
        series_type=series_type,
        is_daily_total=is_daily_total,
        route=route,
    )


def _held(db: Session, user_id: UUID, at: datetime = T0) -> tuple[float, int | None]:
    row = db.execute(
        select(DataPointSeries.value, DataPointSeries.route_id).where(
            DataPointSeries.recorded_at == at,
            DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
        )
    ).one()
    return float(row.value), row.route_id


class TestRoutePrecedence:
    @pytest.fixture
    def repo(self) -> DataPointSeriesRepository:
        return DataPointSeriesRepository(DataPointSeries)

    @pytest.mark.parametrize("all_day_first", [True, False])
    def test_a_workout_sample_keeps_its_second_whichever_arrives_first(
        self, db: Session, repo: DataPointSeriesRepository, all_day_first: bool
    ) -> None:
        user = UserFactory()
        workout = _sample(user.id, 141, SampleRoute.GARMIN_ACTIVITY_DETAILS)
        all_day = _sample(user.id, 128, SampleRoute.GARMIN_DAILIES)

        for batch in ([all_day], [workout]) if all_day_first else ([workout], [all_day]):
            repo.bulk_create(db, batch)

        assert _held(db, user.id) == (141.0, route_id_for(SampleRoute.GARMIN_ACTIVITY_DETAILS))

    @pytest.mark.parametrize("order", ["workout_last", "workout_first"])
    def test_within_one_batch_the_higher_ranked_row_is_the_one_written(
        self, db: Session, repo: DataPointSeriesRepository, order: str
    ) -> None:
        user = UserFactory()
        workout = _sample(user.id, 141, SampleRoute.GARMIN_ACTIVITY_DETAILS)
        all_day = _sample(user.id, 128, SampleRoute.GARMIN_DAILIES)

        repo.bulk_create(db, [all_day, workout] if order == "workout_last" else [workout, all_day])

        assert _held(db, user.id) == (141.0, route_id_for(SampleRoute.GARMIN_ACTIVITY_DETAILS))

    def test_two_feeds_of_equal_rank_keep_last_write_wins(self, db: Session, repo: DataPointSeriesRepository) -> None:
        user = UserFactory()
        repo.bulk_create(db, [_sample(user.id, 100, SampleRoute.GARMIN_DAILIES)])
        repo.bulk_create(db, [_sample(user.id, 104, SampleRoute.GARMIN_EPOCHS)])

        assert _held(db, user.id) == (104.0, route_id_for(SampleRoute.GARMIN_EPOCHS))

    def test_a_resync_of_the_same_feed_still_updates_its_value(
        self, db: Session, repo: DataPointSeriesRepository
    ) -> None:
        user = UserFactory()
        repo.bulk_create(db, [_sample(user.id, 140, SampleRoute.GARMIN_ACTIVITY_DETAILS)])
        repo.bulk_create(db, [_sample(user.id, 142, SampleRoute.GARMIN_ACTIVITY_DETAILS)])

        assert _held(db, user.id) == (142.0, route_id_for(SampleRoute.GARMIN_ACTIVITY_DETAILS))

    def test_a_row_written_before_routes_takes_the_feed_that_delivers_it_again(
        self, db: Session, repo: DataPointSeriesRepository
    ) -> None:
        user = UserFactory()
        repo.bulk_create(db, [_sample(user.id, 128, None)])
        repo.bulk_create(db, [_sample(user.id, 128, SampleRoute.GARMIN_DAILIES)])

        assert _held(db, user.id) == (128.0, route_id_for(SampleRoute.GARMIN_DAILIES))

    def test_a_writer_stating_no_feed_cannot_overwrite_one_that_did(
        self, db: Session, repo: DataPointSeriesRepository
    ) -> None:
        user = UserFactory()
        repo.bulk_create(db, [_sample(user.id, 141, SampleRoute.GARMIN_ACTIVITY_DETAILS)])
        repo.bulk_create(db, [_sample(user.id, 99, None)])

        assert _held(db, user.id) == (141.0, route_id_for(SampleRoute.GARMIN_ACTIVITY_DETAILS))

    def test_a_daily_total_against_a_sample_keeps_last_write_wins(
        self, db: Session, repo: DataPointSeriesRepository
    ) -> None:
        user = UserFactory()
        midnight = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
        repo.bulk_create(
            db,
            [_sample(user.id, 310, SampleRoute.GARMIN_EPOCHS, at=midnight, series_type=SeriesType.steps)],
        )
        repo.bulk_create(
            db,
            [
                _sample(
                    user.id,
                    11_204,
                    SampleRoute.GARMIN_DAILIES,
                    at=midnight,
                    series_type=SeriesType.steps,
                    is_daily_total=True,
                )
            ],
        )

        row = db.execute(
            select(DataPointSeries.value, DataPointSeries.is_daily_total).where(DataPointSeries.recorded_at == midnight)
        ).one()
        assert (float(row.value), row.is_daily_total) == (11_204.0, True)

    def test_raw_timeseries_says_which_feed_wrote_each_sample(
        self, db: Session, repo: DataPointSeriesRepository
    ) -> None:
        user = UserFactory()
        repo.bulk_create(
            db,
            [
                _sample(user.id, 141, SampleRoute.GARMIN_ACTIVITY_DETAILS),
                _sample(user.id, 99, SampleRoute.GARMIN_DAILIES, at=T0 + timedelta(minutes=30)),
                _sample(user.id, 77, None, at=T0 + timedelta(minutes=45)),
            ],
        )

        page = timeseries_service.get_timeseries(
            db,
            user.id,
            [SeriesType.heart_rate],
            TimeSeriesQueryParams(start_datetime=T0, end_datetime=T0 + timedelta(hours=1)),
        )

        assert [(s.value, s.route, s.route_kind) for s in page.data] == [
            (141, "garmin.activity_details", "workout"),
            (99, "garmin.dailies", "window"),
            (77, None, None),
        ]
