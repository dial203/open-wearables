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


ALGORITHM_VERSION = [{"key": "HKAlgorithmVersion", "value": "3"}]


class TestHrvWindowAndMetadata:
    """An export's HRV record keeps its window length and HealthKit metadata.

    The window is the sample's start to end, and a chest-strap reference has to be cut to
    exactly that span; the algorithm version says which Apple method produced the value.
    """

    def test_the_window_length_and_algorithm_version_are_kept(self, service: XMLService) -> None:
        record = _hrv_record("HKQuantityTypeIdentifierHeartRateVariabilityRMSSD")

        sample = service._create_record(record, uuid4(), ALGORITHM_VERSION)

        assert sample is not None
        assert sample.provider_metadata == {"HKAlgorithmVersion": "3", "interval_seconds": 300}

    def test_a_record_with_no_span_states_no_window(self, service: XMLService) -> None:
        record = {
            **_hrv_record("HKQuantityTypeIdentifierHeartRateVariabilitySDNN"),
            "endDate": "2026-09-20 03:10:00 -0400",
        }

        sample = service._create_record(record, uuid4())

        assert sample is not None
        assert sample.provider_metadata is None

    def test_other_record_types_still_keep_no_metadata(self, service: XMLService) -> None:
        record = {**_hrv_record("HKQuantityTypeIdentifierHeartRate"), "unit": "count/min", "value": "58"}

        sample = service._create_record(record, uuid4(), [{"key": "HKMetadataKeyHeartRateMotionContext", "value": "1"}])

        assert sample is not None
        assert sample.provider_metadata is None

    def test_the_parser_hands_each_record_its_own_metadata_entries(self, tmp_path: Path) -> None:
        export = tmp_path / "export.xml"
        export.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilityRMSSD" sourceName="MD Apple Watch Ultra 4" unit="ms"
   startDate="2026-09-28 10:40:05 -0400" endDate="2026-09-28 10:45:05 -0400" value="15">
  <MetadataEntry key="HKAlgorithmVersion" value="3"/>
 </Record>
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilityRMSSD" sourceName="MD Apple Watch Ultra 4" unit="ms"
   startDate="2026-09-28 12:40:29 -0400" endDate="2026-09-28 12:41:29 -0400" value="25"/>
</HealthData>
"""
        )
        service = XMLService(export, getLogger(__name__))

        samples = [s for series, _, _ in service.parse_xml(str(uuid4())) for s in series]

        assert [s.provider_metadata for s in samples] == [
            {"HKAlgorithmVersion": "3", "interval_seconds": 300},
            {"interval_seconds": 60},
        ]
