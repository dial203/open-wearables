"""The span an HRV sample summarises, carried as ``interval_seconds``.

An HRV value is a statistic over a window of beats, so the window is part of the
measurement: an RMSSD over five minutes and one over a minute are different numbers, and
a reference recording has to be cut to the same span before it can be compared with
either. HealthKit states the window as the sample's start and end, but a stored row keeps
only its start (``recorded_at``), so the length goes into
``provider_metadata["interval_seconds"]``, which the timeseries API already returns for
Oura's, Garmin's and Polar's HRV windows.

Apple's windows are not fixed: an Ultra 4 on watchOS 27 writes mostly 300 s windows, but
also 60 s to 352 s ones, which is why the length is taken from each sample rather than
assumed.
"""

from datetime import datetime
from typing import Any

from app.schemas.enums import SeriesType

WINDOWED_SERIES: frozenset[SeriesType] = frozenset(
    {
        SeriesType.heart_rate_variability_sdnn,
        SeriesType.heart_rate_variability_rmssd,
    }
)


def with_measurement_window(
    metadata: dict[str, Any] | None,
    series_type: SeriesType,
    start: datetime,
    end: datetime | None,
) -> dict[str, Any] | None:
    """``metadata`` plus the sample's window length, for a windowed series with a real span.

    A sample whose end is missing or not after its start states no window, so it gets
    none rather than a guessed one. Every other series passes through unchanged.
    """
    if series_type not in WINDOWED_SERIES or end is None or end <= start:
        return metadata
    return {**(metadata or {}), "interval_seconds": round((end - start).total_seconds())}
