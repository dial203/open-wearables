"""HRV records on the Apple Health XML path (pure, no DB).

iOS 27 added ``HKQuantityTypeIdentifierHeartRateVariabilityRMSSD`` alongside the
SDNN type HealthKit has carried since iOS 11. An unmapped type is dropped before
it is counted as skipped, so an export full of RMSSD imported "successfully" with
none of it stored.
"""

from decimal import Decimal
from logging import getLogger
from pathlib import Path
from uuid import uuid4

import pytest

from app.schemas.enums import SeriesType
from app.services.providers.apple.apple_xml.xml_service import XMLService


@pytest.fixture
def service() -> XMLService:
    """The record builder under test never touches the path."""
    return XMLService(Path("unused.xml"), getLogger(__name__))


def _hrv_record(metric_type: str) -> dict[str, str]:
    return {
        "type": metric_type,
        "sourceName": "Apple Watch",
        "unit": "ms",
        "startDate": "2026-09-20 03:10:00 -0400",
        "endDate": "2026-09-20 03:15:00 -0400",
        "value": "42.5",
        "device": (
            "<<HKDevice: 0x1>, name:Apple Watch, manufacturer:Apple Inc., "
            "model:Watch, hardware:Watch7,17, software:27.0>"
        ),
    }


@pytest.mark.parametrize(
    ("metric_type", "series_type"),
    [
        ("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", SeriesType.heart_rate_variability_sdnn),
        ("HKQuantityTypeIdentifierHeartRateVariabilityRMSSD", SeriesType.heart_rate_variability_rmssd),
    ],
)
def test_each_hrv_statistic_lands_in_its_own_series(
    service: XMLService, metric_type: str, series_type: SeriesType
) -> None:
    sample = service._create_record(_hrv_record(metric_type), uuid4())

    assert sample is not None
    assert sample.series_type == series_type
    assert sample.value == Decimal("42.5")
