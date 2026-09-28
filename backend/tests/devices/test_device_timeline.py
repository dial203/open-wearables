"""An account's dated device history: what it resolves to, where ingest files by it,
and what a re-file will and will not move.

The failure these guard is invisible once written: a night filed under the wrong
model looks exactly like that model's nights. So each test pins down which data
source a row ends up on, not merely that it was stored.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import (
    DataPointSeries,
    DataPointSeriesArchive,
    DataSource,
    DeviceHistory,
    EventRecord,
    HealthScore,
    User,
    UserConnection,
    UserConnectionDevicePeriod,
)
from app.repositories.data_point_series_repository import DataPointSeriesRepository
from app.repositories.event_record_repository import EventRecordRepository
from app.schemas.enums import AggregationMethod, DeviceModelOrigin, ProviderName, SeriesType
from app.schemas.model_crud.activities import EventRecordCreate, TimeSeriesSampleCreate
from app.schemas.model_crud.user_management.device_timeline import (
    DevicePeriodInput,
    DeviceRefileRequest,
    DeviceTimelineUpdate,
)
from app.services.device_timeline_service import DeviceTimelineError, device_timeline_service
from app.utils.device_timeline import DevicePeriod, DeviceTimeline
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    HealthScoreFactory,
    SeriesTypeDefinitionFactory,
    UserConnectionFactory,
    UserFactory,
)

SWITCH = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
BEFORE = SWITCH - timedelta(days=2)  # 20 Sep
AFTER = SWITCH + timedelta(days=2)  # 24 Sep


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _sources(db: Session, connection: UserConnection) -> dict[str | None, DataSource]:
    rows = db.query(DataSource).filter(DataSource.user_connection_id == connection.id).all()
    return {row.device_model: row for row in rows}


def _state(db: Session, connection: UserConnection, *periods: tuple[str, datetime | None]) -> None:
    device_timeline_service.replace_timeline(
        db,
        connection,
        DeviceTimelineUpdate(
            periods=[DevicePeriodInput(device_label=label, effective_from=start) for label, start in periods]
        ),
    )


def _sleep(
    user: User, connection: UserConnection, start: datetime, device_model: str | None = None
) -> EventRecordCreate:
    return EventRecordCreate(
        id=uuid4(),
        user_id=user.id,
        provider=ProviderName.GARMIN.value,
        user_connection_id=connection.id,
        source="garmin",
        device_model=device_model,
        category="sleep",
        type="sleep_session",
        source_name="Garmin",
        duration_seconds=8 * 3600,
        start_datetime=start,
        end_datetime=start + timedelta(hours=8),
    )


def _hr(user: User, connection: UserConnection, at: datetime) -> TimeSeriesSampleCreate:
    return TimeSeriesSampleCreate(
        id=uuid4(),
        user_id=user.id,
        provider=ProviderName.GARMIN.value,
        user_connection_id=connection.id,
        source="garmin",
        recorded_at=at,
        value=55,
        series_type=SeriesType.heart_rate,
    )


@pytest.fixture
def user(db: Session) -> User:
    return UserFactory()


@pytest.fixture
def garmin(db: Session, user: User) -> UserConnection:
    return UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-personal")


# ── what a timeline resolves to ──────────────────────────────────────────────


class TestResolution:
    timeline = DeviceTimeline((DevicePeriod("fenix 8", None), DevicePeriod("Venu X1", SWITCH)))

    def test_each_instant_takes_the_period_covering_it(self) -> None:
        assert self.timeline.label_at(BEFORE) == "fenix 8"
        assert self.timeline.label_at(AFTER) == "Venu X1"

    def test_a_period_starts_at_its_instant_inclusive(self) -> None:
        assert self.timeline.label_at(SWITCH) == "Venu X1"
        assert self.timeline.label_at(SWITCH - timedelta(microseconds=1)) == "fenix 8"

    def test_no_time_means_the_current_device(self) -> None:
        """What the undated label always meant, so callers without a time behave as before."""
        assert self.timeline.label_at(None) == "Venu X1"

    def test_a_stretch_before_every_stated_start_resolves_to_nothing(self) -> None:
        """Nearest-period would be a guess indistinguishable from a statement."""
        dated_only = DeviceTimeline((DevicePeriod("Venu X1", SWITCH),))
        assert dated_only.label_at(BEFORE) is None

    def test_a_naive_instant_is_read_as_utc(self) -> None:
        assert self.timeline.label_at(SWITCH.replace(tzinfo=None)) == "Venu X1"

    def test_spans_are_half_open_and_adjoin(self) -> None:
        first, second = self.timeline.spans()
        assert (first.start, first.end) == (None, SWITCH)
        assert (second.start, second.end) == (SWITCH, None)

    @pytest.mark.parametrize(
        "periods",
        [
            (DevicePeriod("a", None), DevicePeriod("b", None)),
            (DevicePeriod("a", SWITCH), DevicePeriod("b", None)),
            (DevicePeriod("a", AFTER), DevicePeriod("b", BEFORE)),
            (DevicePeriod("a", SWITCH), DevicePeriod("b", SWITCH)),
        ],
    )
    def test_an_inconsistent_timeline_is_refused(self, periods: tuple[DevicePeriod, ...]) -> None:
        with pytest.raises(ValueError, match="period"):
            DeviceTimeline(periods)


# ── replacing a timeline ─────────────────────────────────────────────────────


class TestReplace:
    def test_periods_are_stored_in_order_and_the_label_follows_the_latest(
        self, db: Session, garmin: UserConnection
    ) -> None:
        _state(db, garmin, ("Venu X1", SWITCH), ("fenix 8", None))

        timeline = device_timeline_service.get_timeline(db, garmin)
        assert [(p.device_label, p.effective_from) for p in timeline.periods] == [
            ("fenix 8", None),
            ("Venu X1", SWITCH),
        ]
        assert timeline.periods[0].effective_to == SWITCH
        assert timeline.periods[1].effective_to is None
        assert garmin.device_label == "Venu X1"

    def test_an_empty_list_removes_the_timeline_and_keeps_the_last_label(
        self, db: Session, garmin: UserConnection
    ) -> None:
        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))
        _state(db, garmin)

        assert db.query(UserConnectionDevicePeriod).filter_by(user_connection_id=garmin.id).count() == 0
        assert garmin.device_label == "Venu X1"

    def test_replacing_does_not_move_stored_data(self, db: Session, user: User, garmin: UserConnection) -> None:
        garmin.device_label = "fenix 8"
        db.commit()
        record = EventRecordRepository(EventRecord).create(db, _sleep(user, garmin, AFTER))

        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))

        db.refresh(record)
        assert record.data_source_id == _sources(db, garmin)["fenix 8"].id


# ── ingest files by the record's own time ────────────────────────────────────


class TestIngest:
    def test_a_single_record_takes_the_label_for_its_own_start(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))
        repo = EventRecordRepository(EventRecord)

        # Written after the switch, recorded before it: a late sync.
        old = repo.create(db, _sleep(user, garmin, BEFORE))
        new = repo.create(db, _sleep(user, garmin, AFTER))

        sources = _sources(db, garmin)
        assert old.data_source_id == sources["fenix 8"].id
        assert new.data_source_id == sources["Venu X1"].id
        assert {s.device_model_origin for s in sources.values()} == {DeviceModelOrigin.LABEL.value}

    def test_a_device_the_provider_named_wins_over_the_timeline(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        _state(db, garmin, ("Venu X1", None))

        record = EventRecordRepository(EventRecord).create(db, _sleep(user, garmin, AFTER, "Forerunner 965"))

        source = db.get(DataSource, record.data_source_id)
        assert source is not None
        assert source.device_model == "Forerunner 965"
        assert source.device_model_origin == DeviceModelOrigin.PROVIDER.value

    def test_an_unstated_stretch_lands_on_a_model_less_source(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        """Not on the nearest period, and not on today's label."""
        _state(db, garmin, ("Venu X1", SWITCH))

        record = EventRecordRepository(EventRecord).create(db, _sleep(user, garmin, BEFORE))

        source = db.get(DataSource, record.data_source_id)
        assert source is not None
        assert source.device_model is None
        assert source.device_model_origin is None

    def test_without_a_timeline_the_undated_label_still_applies(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        garmin.device_label = "fenix 8"
        db.commit()

        record = EventRecordRepository(EventRecord).create(db, _sleep(user, garmin, AFTER))

        source = db.get(DataSource, record.data_source_id)
        assert source is not None
        assert (source.device_model, source.device_model_origin) == ("fenix 8", DeviceModelOrigin.LABEL.value)

    def test_one_sample_batch_spanning_the_switch_lands_on_two_sources(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))

        DataPointSeriesRepository(DataPointSeries).bulk_create(
            db, [_hr(user, garmin, BEFORE), _hr(user, garmin, SWITCH), _hr(user, garmin, AFTER)]
        )
        db.commit()

        sources = _sources(db, garmin)
        per_source = {
            model: db.query(DataPointSeries).filter_by(data_source_id=source.id).count()
            for model, source in sources.items()
        }
        assert per_source == {"fenix 8": 1, "Venu X1": 2}
        assert {s.device_model_origin for s in sources.values()} == {DeviceModelOrigin.LABEL.value}

    def test_a_bulk_sample_before_every_stated_start_does_not_take_todays_label(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        _state(db, garmin, ("Venu X1", SWITCH))

        DataPointSeriesRepository(DataPointSeries).bulk_create(db, [_hr(user, garmin, BEFORE)])
        db.commit()

        assert set(_sources(db, garmin)) == {None}

    def test_one_event_batch_spanning_the_switch_lands_on_two_sources(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))

        EventRecordRepository(EventRecord).bulk_create(db, [_sleep(user, garmin, BEFORE), _sleep(user, garmin, AFTER)])
        db.commit()

        sources = _sources(db, garmin)
        assert set(sources) == {"fenix 8", "Venu X1"}
        for source in sources.values():
            assert db.query(EventRecord).filter_by(data_source_id=source.id).count() == 1

    def test_two_accounts_in_one_batch_keep_their_own_sources(self, db: Session, user: User) -> None:
        """Identical (user, model, source) on two accounts used to share one map entry."""
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left", device_label="fenix 8")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right", device_label="fenix 8")
        db.commit()

        DataPointSeriesRepository(DataPointSeries).bulk_create(db, [_hr(user, left, BEFORE), _hr(user, right, AFTER)])
        db.commit()

        for account in (left, right):
            source = _sources(db, account)["fenix 8"]
            assert db.query(DataPointSeries).filter_by(data_source_id=source.id).count() == 1


# ── re-filing what is already stored ─────────────────────────────────────────


def _refile(db: Session, connection: UserConnection, *, dry_run: bool = True, include: list | None = None):  # noqa: ANN202
    return device_timeline_service.refile(
        db,
        connection,
        DeviceRefileRequest(dry_run=dry_run, include_data_source_ids=include or []),
        actor="test",
    )


@pytest.fixture
def worn_through_the_switch(db: Session, user: User, garmin: UserConnection) -> DataSource:
    """A fortnight on one label, the switch nobody recorded, then the history stated."""
    garmin.device_label = "fenix 8"
    db.commit()
    events = EventRecordRepository(EventRecord)
    events.create(db, _sleep(user, garmin, BEFORE))
    after = events.create(db, _sleep(user, garmin, AFTER))
    DataPointSeriesRepository(DataPointSeries).bulk_create(db, [_hr(user, garmin, BEFORE), _hr(user, garmin, AFTER)])
    db.commit()
    source = _sources(db, garmin)["fenix 8"]
    HealthScoreFactory(data_source=source, user_id=user.id, event_record_id=after.id, recorded_at=AFTER)
    db.commit()

    _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))
    return source


class TestRefile:
    def test_a_dry_run_says_what_would_move_and_moves_nothing(
        self, db: Session, garmin: UserConnection, worn_through_the_switch: DataSource
    ) -> None:
        result = _refile(db, garmin)

        assert result.dry_run
        (move,) = result.moves
        assert (move.from_device_model, move.to_device_model) == ("fenix 8", "Venu X1")
        assert move.to_data_source_id is None  # nothing created in a dry run
        assert (move.moved.event_records, move.moved.samples, move.moved.health_scores) == (1, 1, 1)
        assert set(_sources(db, garmin)) == {"fenix 8"}
        assert db.query(EventRecord).filter_by(data_source_id=worn_through_the_switch.id).count() == 2

    def test_applying_moves_each_row_to_the_device_worn_at_its_time(
        self, db: Session, garmin: UserConnection, worn_through_the_switch: DataSource
    ) -> None:
        result = _refile(db, garmin, dry_run=False)

        sources = _sources(db, garmin)
        venu = sources["Venu X1"]
        assert venu.device_model_origin == DeviceModelOrigin.LABEL.value
        assert result.moves[0].to_data_source_id == venu.id

        stayed = db.query(EventRecord).filter_by(data_source_id=worn_through_the_switch.id).one()
        moved = db.query(EventRecord).filter_by(data_source_id=venu.id).one()
        assert stayed.start_datetime == BEFORE
        assert moved.start_datetime == AFTER
        assert db.query(DataPointSeries).filter_by(data_source_id=venu.id).one().recorded_at == AFTER
        assert db.query(HealthScore).filter_by(data_source_id=venu.id).one().event_record_id == moved.id

        history = db.query(DeviceHistory).filter_by(action="refiled").one()
        assert (history.old_value, history.new_value) == ("fenix 8", "Venu X1")

    def test_applying_twice_moves_nothing_the_second_time(
        self, db: Session, garmin: UserConnection, worn_through_the_switch: DataSource
    ) -> None:
        _refile(db, garmin, dry_run=False)
        again = _refile(db, garmin, dry_run=False)

        assert again.moves == []

    def test_a_row_already_at_the_destination_is_a_conflict_not_a_move(
        self, db: Session, user: User, garmin: UserConnection, worn_through_the_switch: DataSource
    ) -> None:
        venu = DataSourceFactory(
            user=user,
            provider=ProviderName.GARMIN,
            user_connection_id=garmin.id,
            device_model="Venu X1",
            source="garmin",
            device_model_origin=DeviceModelOrigin.LABEL.value,
        )
        EventRecordFactory(
            data_source=venu, category="sleep", start_datetime=AFTER, end_datetime=AFTER + timedelta(hours=8)
        )
        db.commit()

        result = _refile(db, garmin, dry_run=False)

        (move,) = result.moves
        assert move.conflicts.event_records == 1
        assert move.moved.event_records == 0
        assert move.moved.samples == 1
        # Both copies of that night are still there, each where it was.
        assert db.query(EventRecord).filter_by(data_source_id=worn_through_the_switch.id).count() == 2
        # And the score stays with the record that did not move.
        assert db.query(HealthScore).filter_by(data_source_id=worn_through_the_switch.id).count() == 1

    def test_a_source_the_provider_named_is_never_moved(self, db: Session, user: User, garmin: UserConnection) -> None:
        stamped = DataSourceFactory(
            user=user,
            provider=ProviderName.GARMIN,
            user_connection_id=garmin.id,
            device_model="fenix 8",
            source="garmin",
            device_model_origin=DeviceModelOrigin.PROVIDER.value,
        )
        EventRecordFactory(data_source=stamped, start_datetime=AFTER, end_datetime=AFTER + timedelta(hours=1))
        db.commit()
        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))

        result = _refile(db, garmin, dry_run=False, include=[stamped.id])

        (verdict,) = result.sources
        assert not verdict.eligible
        assert result.moves == []

    def test_a_source_of_unrecorded_origin_moves_only_when_named(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        legacy = DataSourceFactory(
            user=user,
            provider=ProviderName.GARMIN,
            user_connection_id=garmin.id,
            device_model="fenix 8",
            source="garmin",
            device_model_origin=None,
        )
        EventRecordFactory(data_source=legacy, start_datetime=AFTER, end_datetime=AFTER + timedelta(hours=8))
        db.commit()
        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))

        assert _refile(db, garmin).moves == []
        assert not _refile(db, garmin).sources[0].eligible

        named = _refile(db, garmin, include=[legacy.id])
        assert named.sources[0].eligible
        assert named.total_moved.event_records == 1

    def test_another_account_is_never_touched(
        self, db: Session, user: User, garmin: UserConnection, worn_through_the_switch: DataSource
    ) -> None:
        other = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-validation")
        theirs = DataSourceFactory(
            user=user,
            provider=ProviderName.GARMIN,
            user_connection_id=other.id,
            device_model="fenix 8",
            source="garmin",
            device_model_origin=DeviceModelOrigin.LABEL.value,
        )
        EventRecordFactory(data_source=theirs, start_datetime=AFTER, end_datetime=AFTER + timedelta(hours=8))
        db.commit()

        _refile(db, garmin, dry_run=False)

        assert db.query(EventRecord).filter_by(data_source_id=theirs.id).count() == 1
        assert set(_sources(db, other)) == {"fenix 8"}

    def test_a_stretch_no_period_covers_is_counted_and_left(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        garmin.device_label = "fenix 8"
        db.commit()
        EventRecordRepository(EventRecord).create(db, _sleep(user, garmin, BEFORE))
        _state(db, garmin, ("Venu X1", SWITCH))

        result = _refile(db, garmin, dry_run=False)

        assert result.uncovered.event_records == 1
        assert result.moves == []
        assert db.query(EventRecord).filter_by(data_source_id=_sources(db, garmin)["fenix 8"].id).count() == 1

    def test_an_archived_day_the_switch_falls_inside_stays_whole(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        source = DataSourceFactory(
            user=user,
            provider=ProviderName.GARMIN,
            user_connection_id=garmin.id,
            device_model="fenix 8",
            source="garmin",
            device_model_origin=DeviceModelOrigin.LABEL.value,
        )
        heart_rate = SeriesTypeDefinitionFactory.get_or_create_heart_rate()
        for day in (_utc(2026, 9, 22), _utc(2026, 9, 24)):  # the switch is 22 Sep 12:00
            db.add(
                DataPointSeriesArchive(
                    id=uuid4(),
                    data_source_id=source.id,
                    series_type_definition_id=heart_rate.id,
                    bucket_start_at=day,
                    aggregation_type=AggregationMethod.AVG,
                    value=Decimal("55"),
                    sample_count=100,
                )
            )
        db.commit()
        _state(db, garmin, ("fenix 8", None), ("Venu X1", SWITCH))

        result = _refile(db, garmin, dry_run=False)

        (move,) = result.moves
        assert move.moved.archive_days == 1
        assert move.archive_days_straddling == 1
        kept = db.query(DataPointSeriesArchive).filter_by(data_source_id=source.id).one()
        assert kept.bucket_start_at == _utc(2026, 9, 22)

    def test_without_a_timeline_there_is_nothing_to_refile_by(self, db: Session, garmin: UserConnection) -> None:
        with pytest.raises(DeviceTimelineError):
            _refile(db, garmin)

    def test_naming_another_accounts_source_is_refused(self, db: Session, user: User, garmin: UserConnection) -> None:
        other = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-else")
        theirs = DataSourceFactory(user=user, provider=ProviderName.GARMIN, user_connection_id=other.id)
        db.commit()
        _state(db, garmin, ("Venu X1", None))

        with pytest.raises(DeviceTimelineError):
            _refile(db, garmin, include=[theirs.id])
