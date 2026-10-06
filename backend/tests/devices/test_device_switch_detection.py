"""Dating an account's devices from the names its provider puts on workouts.

Garmin names the watch on every activity and never on a night, so an account worn with
a run of watches carries its own history in its workouts. These pin down what that
history comes out as, what counts as evidence for it, that applying it moves the
nights and never the workouts, and that a person's corrections survive it.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.integrations.celery.tasks.detect_device_switches_task import detect_device_switches
from app.models import DataSource, EventRecord, User, UserConnection, UserConnectionDevicePeriod
from app.schemas.enums import DeviceModelOrigin, DevicePeriodOrigin, EntrySource, ProviderName
from app.schemas.model_crud.user_management.device_timeline import (
    DeviceDetectRequest,
    DevicePeriodInput,
    DeviceTimelineUpdate,
)
from app.services.device_timeline_service import DeviceTimelineError, device_timeline_service
from app.utils.device_switch_detection import Evidence, detect_periods, device_key, is_evidence_device, sightings
from tests.factories import (
    DataSourceFactory,
    EventRecordFactory,
    UserConnectionFactory,
    UserFactory,
    WorkoutDetailsFactory,
)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


MAR_1, MAR_5, MAR_10 = _utc(2026, 3, 1, 7), _utc(2026, 3, 5, 23), _utc(2026, 3, 10, 7)
JUN_1, JUN_5, JUN_10, JUN_20 = _utc(2026, 6, 1, 7), _utc(2026, 6, 5, 23), _utc(2026, 6, 10, 7), _utc(2026, 6, 20, 9)
SEP_1, SEP_5, SEP_10 = _utc(2026, 9, 1, 7), _utc(2026, 9, 5, 23), _utc(2026, 9, 10, 7)


# ── what the workouts say ────────────────────────────────────────────────────


class TestDetectPeriods:
    def test_each_change_of_device_starts_a_period_at_its_first_workout(self) -> None:
        evidence = [
            Evidence(MAR_1, "fenix 7"),
            Evidence(MAR_10, "fenix 7"),
            Evidence(JUN_1, "fenix 8"),
            Evidence(JUN_10, "fenix 8"),
            Evidence(SEP_1, "Venu X1"),
        ]

        first, second, third = detect_periods(evidence)

        assert (first.device_label, first.effective_from) == ("fenix 7", None)
        assert (second.device_label, second.effective_from) == ("fenix 8", JUN_1)
        assert (third.device_label, third.effective_from) == ("Venu X1", SEP_1)
        # The gap the switch fell in: after the old watch's last workout.
        assert second.previous_last_seen == MAR_10
        assert (second.evidence_count, second.first_seen, second.last_seen) == (2, JUN_1, JUN_10)

    def test_the_order_evidence_arrives_in_does_not_matter(self) -> None:
        evidence = [Evidence(SEP_1, "Venu X1"), Evidence(MAR_1, "fenix 7"), Evidence(JUN_1, "fenix 8")]

        assert [p.device_label for p in detect_periods(evidence)] == ["fenix 7", "fenix 8", "Venu X1"]

    def test_going_back_to_an_earlier_watch_is_a_switch_too(self) -> None:
        evidence = [Evidence(MAR_1, "fenix 7"), Evidence(JUN_1, "fenix 8"), Evidence(SEP_1, "fenix 7")]

        assert [p.device_label for p in detect_periods(evidence)] == ["fenix 7", "fenix 8", "fenix 7"]

    def test_spellings_of_one_model_are_one_device(self) -> None:
        evidence = [Evidence(MAR_1, "fenix 8"), Evidence(JUN_1, "fēnix 8"), Evidence(SEP_1, "Garmin Fenix 8")]

        (only,) = detect_periods(evidence)
        assert (only.device_label, only.evidence_count) == ("fenix 8", 3)
        assert device_key("Garmin fēnix 8") == device_key("FENIX  8")

    def test_after_an_instant_only_later_workouts_count_and_continue_the_device_then(self) -> None:
        evidence = [Evidence(MAR_1, "fenix 7"), Evidence(JUN_1, "fenix 8"), Evidence(SEP_1, "Venu X1")]

        assert detect_periods(evidence, after=JUN_5, current_label="Venu X1") == []
        (switch,) = detect_periods(evidence, after=JUN_5, current_label="fenix 8")
        assert (switch.device_label, switch.effective_from) == ("Venu X1", SEP_1)

    def test_two_devices_at_one_instant_do_not_start_two_periods_there(self) -> None:
        evidence = [Evidence(MAR_1, "fenix 7"), Evidence(JUN_1, "fenix 8"), Evidence(JUN_1, "Venu X1")]

        starts = [p.effective_from for p in detect_periods(evidence)]
        assert starts == [None, JUN_1]

    def test_nothing_named_means_no_history(self) -> None:
        assert detect_periods([]) == []

    def test_sightings_list_each_device_once_in_the_order_first_seen(self) -> None:
        evidence = [Evidence(SEP_1, "Venu X1"), Evidence(MAR_1, "fenix 7"), Evidence(SEP_10, "Venu X1")]

        assert [(s.device_label, s.evidence_count) for s in sightings(evidence)] == [("fenix 7", 1), ("Venu X1", 2)]

    @pytest.mark.parametrize(
        ("model", "counts"),
        [
            ("Forerunner 965", True),
            ("fēnix 8", True),
            ("vívoactive 5", True),
            ("Venu X1", True),
            ("Garmin CIRQA Smart Band", True),
            ("Polar Grit X2 Pro", True),
            ("Edge 830", False),  # a bike computer records rides, not nights
            ("HRM-Pro Plus", False),  # a chest strap
            ("iPhone 15", False),
            (None, False),
        ],
    )
    def test_only_something_worn_overnight_counts(self, model: str | None, counts: bool) -> None:
        assert is_evidence_device(model) is counts


# ── reading and applying it on an account ────────────────────────────────────


@pytest.fixture
def user(db: Session) -> User:
    return UserFactory()


@pytest.fixture
def garmin(db: Session, user: User) -> UserConnection:
    """The latest watch typed as the account's label, as the dialog used to default to."""
    return UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-personal", device_label="Venu X1")


def _source(
    user: User, connection: UserConnection, model: str, origin: str | None = DeviceModelOrigin.PROVIDER.value
) -> DataSource:
    return DataSourceFactory(
        user=user,
        provider=ProviderName.GARMIN,
        user_connection_id=connection.id,
        device_model=model,
        source="garmin",
        device_model_origin=origin,
    )


def _workout(source: DataSource, at: datetime, entry_source: str | None = None) -> EventRecord:
    record = EventRecordFactory(data_source=source, start_datetime=at, end_datetime=at + timedelta(hours=1))
    if entry_source is not None:
        WorkoutDetailsFactory(event_record=record, entry_source=entry_source)
    return record


def _night(source: DataSource, at: datetime) -> EventRecord:
    return EventRecordFactory(
        data_source=source,
        category="sleep",
        type="sleep_session",
        start_datetime=at,
        end_datetime=at + timedelta(hours=8),
    )


@pytest.fixture
def three_watches(db: Session, user: User, garmin: UserConnection) -> dict[str, DataSource]:
    """Three watches in turn, and every night on the latest one's source.

    The latest watch's label is spelled exactly as Garmin spells it, so its nights and
    its workouts share one source of unrecorded origin - the arrangement that left
    nights unmovable before.
    """
    sources = {
        "fenix 7": _source(user, garmin, "fenix 7"),
        "fenix 8": _source(user, garmin, "fenix 8"),
        "Venu X1": _source(user, garmin, "Venu X1", origin=None),
        "Edge 830": _source(user, garmin, "Edge 830"),
    }
    _workout(sources["fenix 7"], MAR_1)
    _workout(sources["fenix 7"], MAR_10)
    _workout(sources["fenix 8"], JUN_1)
    _workout(sources["fenix 8"], JUN_10)
    _workout(sources["Edge 830"], JUN_20)
    _workout(sources["Venu X1"], SEP_1)
    _workout(sources["Venu X1"], SEP_10)
    for at in (MAR_5, JUN_5, SEP_5):
        _night(sources["Venu X1"], at)
    db.commit()
    return sources


def _periods(db: Session, connection: UserConnection) -> list[tuple[str, datetime | None, str]]:
    rows = (
        db.query(UserConnectionDevicePeriod)
        .filter_by(user_connection_id=connection.id)
        .order_by(UserConnectionDevicePeriod.effective_from.asc().nulls_first())
        .all()
    )
    return [(r.device_label, r.effective_from, r.origin) for r in rows]


def _source_of(db: Session, record: EventRecord) -> str | None:
    db.expire_all()
    stored = db.get(EventRecord, record.id)
    assert stored is not None
    source = db.get(DataSource, stored.data_source_id)
    assert source is not None
    return source.device_model


def _nights(db: Session, connection: UserConnection) -> dict[datetime, str | None]:
    rows = (
        db.query(EventRecord.start_datetime, DataSource.device_model)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .filter(DataSource.user_connection_id == connection.id, EventRecord.category == "sleep")
        .all()
    )
    return {at: model for at, model in rows}


def _detected(*rows: tuple[str, datetime | None]) -> list[tuple[str, datetime | None, str]]:
    return [(label, start, DevicePeriodOrigin.DETECTED.value) for label, start in rows]


class TestEvidence:
    def test_a_watchs_workouts_count_and_a_bike_computers_do_not(
        self, db: Session, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        evidence = device_timeline_service.evidence(db, garmin)

        assert [e.device_model for e in evidence] == ["fenix 7", "fenix 7", "fenix 8", "fenix 8", "Venu X1", "Venu X1"]

    def test_a_label_and_a_typed_in_workout_are_not_evidence(
        self, db: Session, user: User, garmin: UserConnection
    ) -> None:
        labelled = _source(user, garmin, "fenix 6", origin=DeviceModelOrigin.LABEL.value)
        _workout(labelled, MAR_1)
        named = _source(user, garmin, "fenix 7")
        _workout(named, JUN_1, entry_source=EntrySource.MANUAL.value)
        _workout(named, SEP_1, entry_source=EntrySource.AUTOMATIC.value)
        db.commit()

        assert [(e.at, e.device_model) for e in device_timeline_service.evidence(db, garmin)] == [(SEP_1, "fenix 7")]

    def test_another_account_is_never_read(self, db: Session, user: User, garmin: UserConnection) -> None:
        other = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-validation")
        _workout(_source(user, other, "Venu X1"), MAR_1)
        db.commit()

        assert device_timeline_service.evidence(db, garmin) == []


class TestDetect:
    def test_a_fresh_account_gets_its_history_and_its_nights_move_to_the_watch_worn(
        self, db: Session, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        result = device_timeline_service.detect(db, garmin)

        assert result.changed
        assert _periods(db, garmin) == _detected(("fenix 7", None), ("fenix 8", JUN_1), ("Venu X1", SEP_1))
        assert garmin.device_label == "Venu X1"
        assert _nights(db, garmin) == {MAR_5: "fenix 7", JUN_5: "fenix 8", SEP_5: "Venu X1"}
        assert result.refile is not None
        assert result.refile.total_moved.event_records == 2

    def test_no_workout_ever_moves(
        self, db: Session, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        workouts = db.query(EventRecord).filter(EventRecord.category == "workout").all()
        before = {w.id: w.data_source_id for w in workouts}

        device_timeline_service.detect(db, garmin)

        db.expire_all()
        assert {w.id: w.data_source_id for w in db.query(EventRecord).filter(EventRecord.category == "workout")} == (
            before
        )

    def test_running_it_again_changes_nothing(
        self, db: Session, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        device_timeline_service.detect(db, garmin)
        again = device_timeline_service.detect(db, garmin)

        assert not again.changed
        assert again.refile is None

    def test_a_new_watch_is_added_and_the_nights_since_follow_it(
        self, db: Session, user: User, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        device_timeline_service.detect(db, garmin)
        new_watch = _source(user, garmin, "Forerunner 970")
        switch = _utc(2026, 9, 20, 7)
        _workout(new_watch, switch)
        # A night after the switch, filed under the old watch before the run that
        # dates it had synced.
        late_night = _night(three_watches["Venu X1"], _utc(2026, 9, 21, 23))
        db.commit()

        result = device_timeline_service.detect(db, garmin)

        assert result.changed
        assert _periods(db, garmin)[-1] == ("Forerunner 970", switch, DevicePeriodOrigin.DETECTED.value)
        assert garmin.device_label == "Forerunner 970"
        assert _source_of(db, late_night) == "Forerunner 970"

    def test_a_provider_that_names_no_device_has_nothing_to_detect(self, db: Session, user: User) -> None:
        whoop = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        db.commit()

        with pytest.raises(DeviceTimelineError):
            device_timeline_service.detect(db, whoop)
        assert device_timeline_service.get_timeline(db, whoop).detection.supported is False


class TestHandEdits:
    def test_saving_detected_periods_unchanged_keeps_their_origin_and_detection(
        self, db: Session, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        device_timeline_service.detect(db, garmin)

        device_timeline_service.replace_timeline(
            db,
            garmin,
            DeviceTimelineUpdate(
                periods=[
                    DevicePeriodInput(device_label="fenix 7", effective_from=None),
                    DevicePeriodInput(device_label="fenix 8", effective_from=JUN_1),
                    DevicePeriodInput(device_label="Venu X1", effective_from=SEP_1),
                ],
                auto=False,
            ),
        )

        assert _periods(db, garmin) == _detected(("fenix 7", None), ("fenix 8", JUN_1), ("Venu X1", SEP_1))
        assert garmin.device_timeline_detect_from is None
        assert garmin.device_timeline_auto is False

    def test_a_corrected_period_is_stated_and_detection_does_not_undo_it(
        self, db: Session, user: User, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        """A stretch where the nights came from a watch the workouts never name."""
        device_timeline_service.detect(db, garmin)
        borrowed = _utc(2026, 4, 1)
        device_timeline_service.replace_timeline(
            db,
            garmin,
            DeviceTimelineUpdate(
                periods=[
                    DevicePeriodInput(device_label="fenix 7", effective_from=None),
                    DevicePeriodInput(device_label="Venu 2", effective_from=borrowed),
                    DevicePeriodInput(device_label="fenix 8", effective_from=JUN_1),
                    DevicePeriodInput(device_label="Venu X1", effective_from=SEP_1),
                ]
            ),
        )
        assert garmin.device_timeline_detect_from is not None
        assert _periods(db, garmin)[1] == ("Venu 2", borrowed, DevicePeriodOrigin.STATED.value)

        result = device_timeline_service.detect(db, garmin)

        assert not result.changed
        assert [label for label, _, _ in _periods(db, garmin)] == ["fenix 7", "Venu 2", "fenix 8", "Venu X1"]

    def test_a_removed_period_is_not_put_back_but_a_later_switch_is_added(
        self, db: Session, user: User, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        device_timeline_service.detect(db, garmin)
        device_timeline_service.replace_timeline(
            db,
            garmin,
            DeviceTimelineUpdate(
                periods=[
                    DevicePeriodInput(device_label="fenix 7", effective_from=None),
                    DevicePeriodInput(device_label="Venu X1", effective_from=SEP_1),
                ]
            ),
        )
        detect_from = garmin.device_timeline_detect_from
        assert detect_from is not None
        later = detect_from + timedelta(days=3)
        _workout(_source(user, garmin, "Forerunner 970"), later)
        db.commit()

        device_timeline_service.detect(db, garmin)

        assert [(label, origin) for label, _, origin in _periods(db, garmin)] == [
            ("fenix 7", DevicePeriodOrigin.DETECTED.value),
            ("Venu X1", DevicePeriodOrigin.DETECTED.value),
            ("Forerunner 970", DevicePeriodOrigin.DETECTED.value),
        ]
        assert _periods(db, garmin)[-1][1] == later

    def test_a_reset_rebuilds_the_whole_history_from_the_workouts(
        self, db: Session, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        device_timeline_service.replace_timeline(
            db, garmin, DeviceTimelineUpdate(periods=[DevicePeriodInput(device_label="Venu X1", effective_from=None)])
        )
        assert device_timeline_service.get_timeline(db, garmin).detection.differs

        result = device_timeline_service.detect(db, garmin, DeviceDetectRequest(reset=True))

        assert result.changed
        assert garmin.device_timeline_detect_from is None
        assert _periods(db, garmin) == _detected(("fenix 7", None), ("fenix 8", JUN_1), ("Venu X1", SEP_1))
        assert _nights(db, garmin) == {MAR_5: "fenix 7", JUN_5: "fenix 8", SEP_5: "Venu X1"}
        assert not result.timeline.detection.differs


class TestReading:
    def test_the_timeline_shows_what_the_workouts_propose_and_the_gap_of_each_switch(
        self, db: Session, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        before = device_timeline_service.get_timeline(db, garmin)

        assert before.periods == []
        assert before.detection.supported
        assert before.detection.differs
        assert [p.device_label for p in before.detection.proposed] == ["fenix 7", "fenix 8", "Venu X1"]
        assert [d.device_label for d in before.detection.devices] == ["fenix 7", "fenix 8", "Venu X1"]

        device_timeline_service.detect(db, garmin)
        after = device_timeline_service.get_timeline(db, garmin)

        assert after.auto
        assert not after.detection.differs
        assert [p.previous_last_seen for p in after.periods] == [None, MAR_10, JUN_10]


class TestSweep:
    def test_only_automatic_accounts_are_updated(
        self, db: Session, user: User, garmin: UserConnection, three_watches: dict[str, DataSource]
    ) -> None:
        frozen = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id="g-frozen", device_timeline_auto=False
        )
        _workout(_source(user, frozen, "fenix 7"), MAR_1)
        db.commit()

        with patch("app.integrations.celery.tasks.detect_device_switches_task.SessionLocal") as session_local:
            session_local.return_value.__enter__ = MagicMock(return_value=db)
            session_local.return_value.__exit__ = MagicMock(return_value=None)
            result = detect_device_switches()

        assert result["changed"] == [str(garmin.id)]
        assert result["failed"] == []
        assert _periods(db, frozen) == []
