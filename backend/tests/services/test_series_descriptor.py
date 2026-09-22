"""Every series says what cadence it actually returned.

A six-second trace and a one-second trace are different instruments. An agreement
analysis handed one in place of the other, or handed either with its cadence
unstated, resamples onto the wrong grid and narrows its limits of agreement by
roughly the square root of the oversampling factor - so the agreement looks better
than it is, and nothing in the payload says otherwise.

The cases below are the real shape of one validation bout (a Garmin Venu X1 run,
84:50 elapsed, eight sources) as measured from a live instance, so a regression
here shows up as the exact wrong answer a consumer would act on.
"""

from datetime import datetime, timedelta, timezone

from app.schemas.enums import ResolutionClass, SeriesType
from app.schemas.responses.activity import TimeSeriesSample
from app.schemas.utils import SourceMetadata
from app.services.timeseries_service import _describe_series

RUN_START = datetime(2026, 9, 20, 0, 1, 0, tzinfo=timezone.utc)


def _series(
    data_source_id: str | None,
    series_type: SeriesType,
    n: int,
    step_seconds: float,
    start: datetime = RUN_START,
) -> list[TimeSeriesSample]:
    """n samples of one series, evenly spaced."""
    return [
        TimeSeriesSample(
            timestamp=start + timedelta(seconds=i * step_seconds),
            type=series_type,
            value=148,
            unit="bpm",
            source=(
                SourceMetadata(provider="test", data_source_id=data_source_id) if data_source_id is not None else None
            ),
        )
        for i in range(n)
    ]


def _by_source(descriptors: list, data_source_id: str) -> dict:
    return next(d for d in descriptors if str(d.data_source_id) == data_source_id)


class TestMeasuredCadence:
    def test_each_source_is_classified_by_its_own_spacing(self) -> None:
        """The eight-source bout: every source keeps its own cadence, none inherits another's."""
        garmin = "11111111-1111-1111-1111-111111111111"
        strava = "22222222-2222-2222-2222-222222222222"
        watch = "33333333-3333-3333-3333-333333333333"
        whoop_relayed = "44444444-4444-4444-4444-444444444444"
        garmin_relayed = "55555555-5555-5555-5555-555555555555"
        oura = "66666666-6666-6666-6666-666666666666"

        samples = (
            _series(garmin, SeriesType.heart_rate, 5033, 1)
            + _series(strava, SeriesType.heart_rate, 4979, 1)
            + _series(watch, SeriesType.heart_rate, 1005, 5)
            + _series(whoop_relayed, SeriesType.heart_rate, 830, 6)
            + _series(garmin_relayed, SeriesType.heart_rate, 42, 120)
            + _series(oura, SeriesType.heart_rate, 9, 600)
        )

        descriptors = _describe_series(samples, truncated=False, server_aggregated=False)
        assert descriptors is not None
        assert len(descriptors) == 6

        expected = {
            garmin: (5033, 1.0, ResolutionClass.PER_SECOND),
            strava: (4979, 1.0, ResolutionClass.PER_SECOND),
            watch: (1005, 5.0, ResolutionClass.SUB_MINUTE),
            whoop_relayed: (830, 6.0, ResolutionClass.SUB_MINUTE),
            garmin_relayed: (42, 120.0, ResolutionClass.MINUTE),
            oura: (9, 600.0, ResolutionClass.COARSE),
        }
        for source_id, (n, interval, klass) in expected.items():
            found = _by_source(descriptors, source_id)
            assert (found.n, found.interval_s_median, found.resolution_class) == (n, interval, klass)

    def test_a_six_second_series_is_never_called_per_second(self) -> None:
        """WHOOP's real HealthKit ceiling. Reporting it as per-second is the whole failure."""
        descriptor = _describe_series(
            _series("77777777-7777-7777-7777-777777777777", SeriesType.heart_rate, 844, 6),
            truncated=False,
            server_aggregated=False,
        )[0]

        assert descriptor.interval_s_median == 6.0
        assert descriptor.resolution_class is ResolutionClass.SUB_MINUTE

    def test_a_dropped_second_still_reads_as_per_second(self) -> None:
        """A 1 Hz recording with holes is still 1 Hz; the median ignores the gaps."""
        start = RUN_START
        offsets = [0, 1, 2, 3, 5, 6, 7, 9, 10]
        samples = [
            TimeSeriesSample(
                timestamp=start + timedelta(seconds=offset),
                type=SeriesType.heart_rate,
                value=150,
                unit="bpm",
                source=SourceMetadata(provider="test", data_source_id="88888888-8888-8888-8888-888888888888"),
            )
            for offset in offsets
        ]

        descriptor = _describe_series(samples, truncated=False, server_aggregated=False)[0]

        assert descriptor.interval_s_median == 1.0
        assert descriptor.resolution_class is ResolutionClass.PER_SECOND
        assert descriptor.n == 9

    def test_one_sample_reports_no_cadence_rather_than_guessing(self) -> None:
        """A single reading has no spacing. Inventing one is how a stale label gets made."""
        descriptor = _describe_series(
            _series("99999999-9999-9999-9999-999999999999", SeriesType.heart_rate, 1, 1),
            truncated=False,
            server_aggregated=False,
        )[0]

        assert descriptor.n == 1
        assert descriptor.interval_s_median is None
        assert descriptor.resolution_class is None

    def test_no_samples_describes_nothing(self) -> None:
        assert _describe_series([], truncated=False, server_aggregated=False) is None

    def test_interbeat_series_are_their_own_class(self) -> None:
        """RR intervals are paced by the heartbeat, not by a clock."""
        descriptor = _describe_series(
            _series("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", SeriesType.rr_interval, 100, 0.85),
            truncated=False,
            server_aggregated=False,
        )[0]

        assert descriptor.resolution_class is ResolutionClass.BEAT_TO_BEAT

    def test_one_source_reporting_two_types_is_described_separately(self) -> None:
        """A watch writing HR at 1 Hz and SpO2 every 10 min is two series, not an average."""
        source_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        samples = _series(source_id, SeriesType.heart_rate, 100, 1) + _series(
            source_id, SeriesType.oxygen_saturation, 6, 600
        )

        descriptors = _describe_series(samples, truncated=False, server_aggregated=False)

        assert len(descriptors) == 2
        by_type = {d.type: d for d in descriptors}
        assert by_type[SeriesType.heart_rate].resolution_class is ResolutionClass.PER_SECOND
        assert by_type[SeriesType.oxygen_saturation].resolution_class is ResolutionClass.COARSE


class TestHonestyFlags:
    def test_a_truncated_page_is_not_complete(self) -> None:
        """More of the series follows the cursor, so the span here is not the whole span."""
        descriptor = _describe_series(
            _series("cccccccc-cccc-cccc-cccc-cccccccccccc", SeriesType.heart_rate, 50, 1),
            truncated=True,
            server_aggregated=False,
        )[0]

        assert descriptor.complete is False

    def test_server_bucketing_is_declared(self) -> None:
        """Otherwise a requested bucket width is indistinguishable from upstream's own cadence."""
        bucketed = _describe_series(
            _series("dddddddd-dddd-dddd-dddd-dddddddddddd", SeriesType.heart_rate, 10, 60),
            truncated=False,
            server_aggregated=True,
        )[0]
        native = _describe_series(
            _series("dddddddd-dddd-dddd-dddd-dddddddddddd", SeriesType.heart_rate, 10, 60),
            truncated=False,
            server_aggregated=False,
        )[0]

        assert bucketed.server_aggregated is True
        assert native.server_aggregated is False
        # Same measured spacing either way - the flag is what tells them apart.
        assert bucketed.interval_s_median == native.interval_s_median == 60.0

    def test_the_span_is_the_samples_own_first_and_last(self) -> None:
        """Not the window that was asked for, which is usually wider than what exists."""
        descriptor = _describe_series(
            _series("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", SeriesType.heart_rate, 61, 1),
            truncated=False,
            server_aggregated=False,
        )[0]

        assert descriptor.start == RUN_START
        assert descriptor.end == RUN_START + timedelta(seconds=60)
