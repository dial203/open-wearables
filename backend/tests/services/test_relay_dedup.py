"""The aggregator's copy of a maker that is also connected directly is left out of reads.

The cases below are the ones that decide whether this rule can be trusted with study
data. Hiding the second copy is the easy half; the half worth testing is everything it
must *not* hide - history from before the direct connection existed, a second unit of
the same brand that only the aggregator sees, a brand with no direct route at all, and
anything a person has said to keep.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, DataSource, User
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import Resolution, SeriesType, get_series_type_id
from app.schemas.model_crud.activities import EventRecordQueryParams, TimeSeriesQueryParams
from app.schemas.responses.activity import TimeSeriesSample
from app.schemas.utils import PaginatedResponse
from app.services.event_record_service import event_record_service
from app.services.sources.relay_dedup import build_event_plan, build_series_plan, classify_sources
from app.services.timeseries_service import timeseries_service
from tests.factories import (
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    SeriesTypeDefinitionFactory,
    UserFactory,
)

HEART_RATE_ID = get_series_type_id(SeriesType.heart_rate)
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _heart_rate(data_source: DataSource, at: datetime, value: int = 60) -> DataPointSeries:
    return DataPointSeriesFactory(
        data_source=data_source,
        series_type=SeriesTypeDefinitionFactory.get_or_create_heart_rate(),
        recorded_at=at,
        value=Decimal(value),
    )


def _direct_oura(user: User) -> DataSource:
    return DataSourceFactory(
        user=user,
        provider="oura",
        device_model="Oura Ring Gen3",
        source="oura_api",
        original_source_name="Oura",
    )


def _relayed_oura(user: User, **kwargs) -> DataSource:
    """Oura data arriving through Apple Health: same brand, different route."""
    return DataSourceFactory(
        user=user,
        provider="apple",
        device_model=None,
        source="com.ouraring.oura",
        original_source_name="Oura",
        **kwargs,
    )


def _params(start: datetime, end: datetime, **kwargs) -> TimeSeriesQueryParams:
    return TimeSeriesQueryParams(start_datetime=start, end_datetime=end, limit=100, **kwargs)


def _read(db: Session, user: User, start: datetime, end: datetime, **kwargs) -> PaginatedResponse[TimeSeriesSample]:
    return timeseries_service.get_timeseries(db, user.id, [SeriesType.heart_rate], _params(start, end, **kwargs))


def _event_params(start: datetime | None = None, end: datetime | None = None) -> EventRecordQueryParams:
    return EventRecordQueryParams(
        start_datetime=start or NOW - timedelta(days=1),
        end_datetime=end or NOW + timedelta(days=1),
        limit=50,
    )


class TestTheRelayedCopyIsLeftOut:
    def test_relayed_samples_are_hidden_where_the_direct_route_covers_them(self, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW, 58)
        _heart_rate(relayed, NOW + timedelta(seconds=30), 58)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        source_ids = {sample.source.data_source_id for sample in page.data}
        assert source_ids == {direct.id}

    def test_what_was_hidden_is_reported_rather_than_silently_missing(self, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        dedup = page.metadata.relay_dedup
        assert dedup is not None
        assert dedup.applied
        hidden = {entry.data_source_id: entry for entry in dedup.hidden}
        assert relayed.id in hidden
        assert hidden[relayed.id].direct_provider == "oura"
        assert hidden[relayed.id].reason == "covered_by_direct"

    def test_asking_for_the_relayed_copies_returns_them_unchanged(self, db: Session) -> None:
        """Nothing is deleted at ingest, so the data is always one query parameter away."""
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1), include_redundant_relays=True)

        assert {sample.source.data_source_id for sample in page.data} == {direct.id, relayed.id}
        assert page.metadata.relay_dedup is None

    def test_downsampled_reads_hide_the_same_rows_as_raw_ones(self, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        for minute in range(10):
            _heart_rate(direct, NOW + timedelta(minutes=minute), 55)
            _heart_rate(relayed, NOW + timedelta(minutes=minute, seconds=20), 55)
        db.commit()

        page = timeseries_service.get_timeseries(
            db,
            user.id,
            [SeriesType.heart_rate],
            _params(NOW - timedelta(hours=1), NOW + timedelta(hours=1), resolution=Resolution.FIVE_MIN),
        )

        assert {sample.source.data_source_id for sample in page.data} == {direct.id}

    def test_a_read_naming_one_source_returns_that_source(self, db: Session) -> None:
        """Asking for a source by id is asking for it, whatever the rule would say."""
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1), data_source_id=relayed.id)

        assert [sample.source.data_source_id for sample in page.data] == [relayed.id]


class TestWhatItMustNotHide:
    def test_history_from_before_the_direct_connection_stays_visible(self, db: Session) -> None:
        """Connecting Oura today must not blank out last year's Apple-relayed Oura."""
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW - timedelta(days=200), 61)
        db.commit()

        page = _read(db, user, NOW - timedelta(days=365), NOW + timedelta(hours=1))

        assert relayed.id in {sample.source.data_source_id for sample in page.data}

    def test_samples_after_the_direct_route_stopped_delivering_stay_visible(self, db: Session) -> None:
        """An expired token hands visibility back without anyone touching a setting."""
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW - timedelta(days=30))
        _heart_rate(relayed, NOW - timedelta(days=30, seconds=15))
        _heart_rate(relayed, NOW, 62)
        db.commit()

        page = _read(db, user, NOW - timedelta(days=60), NOW + timedelta(hours=1))

        by_source: dict = {}
        for sample in page.data:
            by_source.setdefault(sample.source.data_source_id, []).append(sample.timestamp)
        assert len(by_source[relayed.id]) == 1
        assert by_source[relayed.id][0] == NOW

    def test_a_series_type_the_direct_route_never_delivers_stays_visible(self, db: Session) -> None:
        """Coverage is measured per series type, not per brand."""
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW)
        DataPointSeriesFactory(
            data_source=relayed,
            series_type=SeriesTypeDefinitionFactory.get_or_create_steps(),
            recorded_at=NOW,
            value=Decimal(4200),
        )
        db.commit()

        page = timeseries_service.get_timeseries(
            db, user.id, [SeriesType.steps], _params(NOW - timedelta(hours=1), NOW + timedelta(hours=1))
        )

        assert {sample.source.data_source_id for sample in page.data} == {relayed.id}

    def test_a_brand_with_no_direct_route_is_never_hidden(self, db: Session) -> None:
        """A third-party HealthKit writer can only ever reach us relayed."""
        user = UserFactory()
        direct = _direct_oura(user)
        zepp = DataSourceFactory(
            user=user,
            provider="apple",
            device_model=None,
            source="Zepp Life",
            original_source_name="Zepp",
        )
        _heart_rate(direct, NOW)
        _heart_rate(zepp, NOW, 63)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        assert zepp.id in {sample.source.data_source_id for sample in page.data}

    def test_the_aggregators_own_brand_is_never_hidden(self, db: Session) -> None:
        """An Apple Watch inside Apple Health is first-party data, not a relay."""
        user = UserFactory()
        apple_watch = DataSourceFactory(
            user=user,
            provider="apple",
            device_model="Watch7,5",
            source="com.apple.health.ABC",
            original_source_name="Apple",
        )
        _heart_rate(_direct_oura(user), NOW)
        _heart_rate(apple_watch, NOW, 64)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        assert apple_watch.id in {sample.source.data_source_id for sample in page.data}

    def test_a_second_unit_seen_only_through_the_aggregator_stays_visible(self, db: Session) -> None:
        """Two rings, one connected: hiding the relayed one would lose that unit entirely."""
        user = UserFactory()
        direct = _direct_oura(user)
        repo = DeviceRepository()
        connected_ring = repo.create(db, user_id=user.id, device_type="ring", brand="Oura")
        spare_ring = repo.create(db, user_id=user.id, device_type="ring", brand="Oura")
        db.flush()
        direct.device_id = connected_ring.id
        relayed = _relayed_oura(user, device_id=spare_ring.id)
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW, 65)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        assert relayed.id in {sample.source.data_source_id for sample in page.data}

    def test_another_users_direct_connection_hides_nothing(self, db: Session) -> None:
        user = UserFactory()
        other = UserFactory()
        _direct_oura(other)
        relayed = _relayed_oura(user)
        _heart_rate(relayed, NOW)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        assert {sample.source.data_source_id for sample in page.data} == {relayed.id}


class TestTheGlobalSwitch:
    """RELAY_DEDUP_ENABLED=false must put the old behaviour back, everywhere, at once."""

    def test_nothing_is_hidden_when_the_rule_is_switched_off(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "relay_dedup_enabled", False)
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        assert {sample.source.data_source_id for sample in page.data} == {direct.id, relayed.id}
        assert page.metadata.relay_dedup is None

    def test_a_hand_hidden_source_comes_back_too(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        """The switch is the whole rule, overrides included - one thing to reason about."""
        monkeypatch.setattr(settings, "relay_dedup_enabled", False)
        user = UserFactory()
        relayed = _relayed_oura(user, relay_visibility="never")
        _heart_rate(relayed, NOW)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        assert {sample.source.data_source_id for sample in page.data} == {relayed.id}

    def test_events_follow_the_same_switch(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "relay_dedup_enabled", False)
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        EventRecordFactory(data_source=direct, category="workout", start_datetime=NOW)
        EventRecordFactory(data_source=relayed, category="workout", start_datetime=NOW + timedelta(minutes=3))
        db.commit()

        page = event_record_service.get_workouts(db, user.id, _event_params())

        assert {workout.source.data_source_id for workout in page.data} == {direct.id, relayed.id}


class TestSleepUnderPriorityFiltering:
    """Ranking and reading have to agree, or a night ranked to a hidden relay reads empty."""

    def test_a_night_both_routes_recorded_comes_back_from_the_direct_source(self, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        EventRecordFactory(
            data_source=direct, category="sleep", type="asleep", start_datetime=NOW, duration_seconds=25200
        )
        EventRecordFactory(
            data_source=relayed,
            category="sleep",
            type="asleep",
            start_datetime=NOW + timedelta(minutes=4),
            duration_seconds=25200,
        )
        db.commit()

        page = event_record_service.get_sleep_sessions(db, user.id, _event_params(), filter_by_priority=True)

        assert [session.source.data_source_id for session in page.data] == [direct.id]

    def test_a_night_only_the_relay_recorded_is_not_lost(self, db: Session) -> None:
        """Outside the direct route's span the relay still ranks, and still wins."""
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        EventRecordFactory(
            data_source=direct,
            category="sleep",
            type="asleep",
            start_datetime=NOW - timedelta(days=10),
            duration_seconds=25200,
        )
        EventRecordFactory(
            data_source=relayed, category="sleep", type="asleep", start_datetime=NOW, duration_seconds=25200
        )
        db.commit()

        page = event_record_service.get_sleep_sessions(
            db,
            user.id,
            _event_params(start=NOW - timedelta(days=30), end=NOW + timedelta(days=1)),
            filter_by_priority=True,
        )

        assert relayed.id in {session.source.data_source_id for session in page.data}


class TestTheManualOverride:
    def test_always_keeps_a_relayed_source_in_every_read(self, db: Session) -> None:
        user = UserFactory()
        direct = _direct_oura(user)
        relayed = _relayed_oura(user, relay_visibility="always")
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW, 66)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        assert relayed.id in {sample.source.data_source_id for sample in page.data}

    def test_never_hides_a_source_even_with_no_direct_route(self, db: Session) -> None:
        user = UserFactory()
        relayed = _relayed_oura(user, relay_visibility="never")
        kept = DataSourceFactory(user=user, provider="apple", source="com.apple.health.ABC")
        _heart_rate(relayed, NOW)
        _heart_rate(kept, NOW, 67)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        source_ids = {sample.source.data_source_id for sample in page.data}
        assert relayed.id not in source_ids
        assert kept.id in source_ids

    def test_a_hand_hidden_source_is_reported_as_such(self, db: Session) -> None:
        user = UserFactory()
        relayed = _relayed_oura(user, relay_visibility="never")
        _heart_rate(relayed, NOW)
        db.commit()

        page = _read(db, user, NOW - timedelta(hours=1), NOW + timedelta(hours=1))

        dedup = page.metadata.relay_dedup
        assert dedup is not None
        assert [entry.reason for entry in dedup.hidden] == ["hidden_by_hand"]


class TestEventReads:
    def test_a_workout_recorded_twice_is_returned_once(self, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        EventRecordFactory(data_source=direct, category="workout", start_datetime=NOW, source_name="Oura")
        EventRecordFactory(
            data_source=relayed,
            category="workout",
            start_datetime=NOW + timedelta(minutes=3),
            source_name="Oura",
        )
        db.commit()

        page = event_record_service.get_workouts(db, user.id, _event_params())

        assert [workout.source.data_source_id for workout in page.data] == [direct.id]
        assert page.metadata.relay_dedup is not None

    def test_a_category_the_direct_route_never_delivers_stays_visible(self, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        EventRecordFactory(data_source=direct, category="workout", start_datetime=NOW)
        EventRecordFactory(data_source=relayed, category="sleep", type="sleep", start_datetime=NOW)
        db.commit()

        plan = build_event_plan(db, user.id, ["sleep"], NOW - timedelta(days=1), NOW + timedelta(days=1))

        assert not plan.applied


class TestThePlanItself:
    def test_no_plan_when_the_user_has_only_direct_sources(self, db: Session) -> None:
        user = UserFactory()
        _heart_rate(_direct_oura(user), NOW)
        db.commit()

        plan = build_series_plan(db, user.id, [HEART_RATE_ID], NOW - timedelta(days=1), NOW + timedelta(days=1))

        assert not plan.applied
        assert plan.hidden_source_ids == frozenset()

    def test_a_typeless_plan_falls_back_to_one_span_per_brand(self, db: Session) -> None:
        """A read naming no series type still gets a rule, just a coarser one."""
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        _heart_rate(direct, NOW)
        _heart_rate(relayed, NOW)
        db.commit()

        plan = build_series_plan(db, user.id, [], NOW - timedelta(days=1), NOW + timedelta(days=1))

        assert plan.applied
        assert plan.hidden_source_ids == frozenset({relayed.id})

    def test_sources_are_classified_without_touching_the_sample_tables(self, db: Session) -> None:
        user = UserFactory()
        direct, relayed = _direct_oura(user), _relayed_oura(user)
        db.commit()

        statuses = classify_sources(db, user.id)

        assert direct.id not in statuses
        assert statuses[relayed.id].redundant
        assert statuses[relayed.id].direct_provider == "oura"
