from datetime import timedelta
from enum import StrEnum


class DataGranularity(StrEnum):
    """How finely a provider's 24/7 data is stored.

    DAILY  — one aggregated value per day (server-side rollup).
    HOURLY — one aggregated value per hour (server-side rollup).
    RAW    — every individual reading (no aggregation), where the provider supports it.
    """

    DAILY = "daily"
    HOURLY = "hourly"
    RAW = "raw"


# Aggregation window (seconds) per aggregating granularity.
# Raw is absent intentionally
# Add an entry here when adding a granularity that aggregates.
GRANULARITY_WINDOW_SECONDS: dict[DataGranularity, int] = {
    DataGranularity.DAILY: 86_400,
    DataGranularity.HOURLY: 3_600,
}


class Resolution(StrEnum):
    """Bucket width requested when reading time series. RAW returns stored samples untouched."""

    RAW = "raw"
    ONE_MIN = "1min"
    FIVE_MIN = "5min"
    FIFTEEN_MIN = "15min"
    ONE_HOUR = "1hour"


BUCKET_SIZES: dict[Resolution, timedelta] = {
    Resolution.ONE_MIN: timedelta(minutes=1),
    Resolution.FIVE_MIN: timedelta(minutes=5),
    Resolution.FIFTEEN_MIN: timedelta(minutes=15),
    Resolution.ONE_HOUR: timedelta(hours=1),
}


class ResolutionClass(StrEnum):
    """What a returned series is, as an instrument, judged on its measured spacing.

    A 6-second series and a 1-second series are different instruments, and an
    agreement analysis that resamples one onto the other's grid manufactures
    autocorrelation and narrows its limits of agreement. So a consumer has to be
    able to tell them apart without counting samples and guessing the span, which
    is what this classifies.

    ``BEAT_TO_BEAT`` is not a spacing threshold: it names a series whose samples
    are inter-beat intervals (RR from an ECG strap, pulse-to-pulse from an optical
    sensor), where the cadence is the heartbeat rather than a clock. Which of the
    two it was stays in the series type, because that distinction is provenance,
    not resolution.
    """

    BEAT_TO_BEAT = "beat_to_beat"
    PER_SECOND = "per_second"
    SUB_MINUTE = "sub_minute"
    MINUTE = "minute"
    COARSE = "coarse"


# Upper bound (inclusive, seconds) of median sample spacing for each class.
# per_second is 1.5 rather than 1.0 so a 1 Hz recording with the occasional
# dropped second still reads as per-second, while a 2 s series never does.
PER_SECOND_MAX_INTERVAL_S = 1.5
SUB_MINUTE_MAX_INTERVAL_S = 60.0
MINUTE_MAX_INTERVAL_S = 300.0


def classify_interval(interval_s: float, *, interbeat: bool = False) -> ResolutionClass:
    """Classify a measured median sample spacing.

    ``interbeat`` marks a series of inter-beat intervals, which is a class of its
    own regardless of how far apart the beats happen to be.
    """
    if interbeat:
        return ResolutionClass.BEAT_TO_BEAT
    if interval_s <= PER_SECOND_MAX_INTERVAL_S:
        return ResolutionClass.PER_SECOND
    if interval_s <= SUB_MINUTE_MAX_INTERVAL_S:
        return ResolutionClass.SUB_MINUTE
    if interval_s <= MINUTE_MAX_INTERVAL_S:
        return ResolutionClass.MINUTE
    return ResolutionClass.COARSE
