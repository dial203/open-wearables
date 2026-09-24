"""Unit tests for app.algorithms.sleep_onset — latency and WASO from stage intervals."""

from datetime import datetime, timedelta, timezone

from app.algorithms.sleep_onset import SHORT_FIRST_SLEEP_RUN_MINUTES, derive_sleep_onset_metrics
from app.schemas.model_crud.activities.sleep import SleepStage

T0 = datetime(2026, 9, 21, 23, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def stage(kind: str, start: float, end: float) -> SleepStage:
    return SleepStage(stage=kind, start_time=at(start), end_time=at(end))


class TestDeriveSleepOnsetMetrics:
    def test_splits_total_wake_into_latency_and_waso(self) -> None:
        m = derive_sleep_onset_metrics(
            [
                stage("awake", 0, 15),
                stage("light", 15, 120),
                stage("awake", 120, 130),
                stage("deep", 130, 470),
                stage("awake", 470, 480),
            ],
            T0,
        )
        assert m is not None
        assert m.onset_time == at(15)
        assert m.latency_minutes == 15
        assert m.waso_minutes == 20
        assert m.terminal_wake_minutes == 10
        assert m.total_awake_minutes == 35
        assert m.awakenings == 2
        assert m.unaccounted_after_onset_minutes == 0
        assert m.short_first_sleep_run is False

    def test_onset_is_the_first_sleep_epoch_and_a_short_run_is_flagged(self) -> None:
        m = derive_sleep_onset_metrics(
            [
                stage("awake", 0, 10),
                stage("light", 10, 10.5),
                stage("awake", 10.5, 30),
                stage("light", 30, 300),
            ],
            T0,
        )
        assert m is not None
        assert m.latency_minutes == 10
        assert m.waso_minutes == 19.5
        assert m.first_sleep_run_minutes == 0.5
        assert m.short_first_sleep_run is True
        assert SHORT_FIRST_SLEEP_RUN_MINUTES == 10

    def test_touching_sleep_stages_are_one_run(self) -> None:
        m = derive_sleep_onset_metrics(
            [stage("light", 0, 5), stage("deep", 5, 12), stage("rem", 12, 20), stage("awake", 20, 25)],
            T0,
        )
        assert m is not None
        assert m.first_sleep_run_minutes == 20

    def test_unscored_time_after_onset_is_not_wake(self) -> None:
        m = derive_sleep_onset_metrics(
            [
                stage("awake", 0, 10),
                stage("light", 10, 120),
                stage("unknown", 120, 134),
                # 134–140 is a gap no interval covers
                stage("light", 140, 420),
            ],
            T0,
        )
        assert m is not None
        assert m.waso_minutes == 0
        assert m.unaccounted_after_onset_minutes == 20

    def test_no_latency_when_the_run_up_is_not_scored_awake(self) -> None:
        m = derive_sleep_onset_metrics([stage("unknown", 0, 10), stage("light", 10, 300)], T0)
        assert m is not None
        assert m.latency_minutes is None
        assert m.waso_minutes == 0

    def test_in_bed_is_a_window_not_a_stage(self) -> None:
        # HealthKit publishes in_bed spanning the whole session. Read as sleep it
        # would put onset at the session start and fold the latency into WASO.
        m = derive_sleep_onset_metrics(
            [stage("in_bed", 0, 480), stage("awake", 0, 20), stage("light", 20, 480)],
            T0,
        )
        assert m is not None
        assert m.latency_minutes == 20
        assert m.waso_minutes == 0

    def test_unordered_intervals_give_the_same_answer(self) -> None:
        ordered = [stage("awake", 0, 15), stage("light", 15, 120), stage("awake", 120, 130), stage("deep", 130, 400)]
        assert derive_sleep_onset_metrics(list(reversed(ordered)), T0) == derive_sleep_onset_metrics(ordered, T0)

    def test_no_session_start_leaves_latency_null(self) -> None:
        m = derive_sleep_onset_metrics([stage("awake", 0, 10), stage("light", 10, 300)], None)
        assert m is not None
        assert m.latency_minutes is None
        assert m.waso_minutes == 0

    def test_nothing_derived_without_sleep(self) -> None:
        assert derive_sleep_onset_metrics([stage("awake", 0, 40)], T0) is None
        assert derive_sleep_onset_metrics([], T0) is None
        assert derive_sleep_onset_metrics(None, T0) is None
