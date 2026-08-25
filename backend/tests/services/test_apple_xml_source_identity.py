"""Source identity on the Apple Health XML sleep path (pure, no DB).

Sleep records in the Apple export carry no ``device`` attribute — only
``sourceName``. The parser read only ``device``, so every sleep record in an
export resolved to an empty ``SourceInfo``: no name, no model. A whole Apple
Watch history imported as one nameless session stream while the import reported
complete success, because the records *were* read — they just lost the one
attribute saying whose night it was.
"""

from logging import getLogger
from pathlib import Path

import pytest

from app.services.apple.apple_xml.xml_service import XMLService

logger = getLogger(__name__)


@pytest.fixture
def service() -> XMLService:
    """The parse helpers under test never touch the path."""
    return XMLService(Path("unused.xml"), logger)


# A real sleep record from an Apple Health export: sourceName, no device.
WATCH_SLEEP_RECORD = {
    "type": "HKCategoryTypeIdentifierSleepAnalysis",
    "sourceName": "Apple Watch Ultra 3 Current",
    "sourceVersion": "27.0",
    "startDate": "2026-08-24 06:35:43 -0400",
    "endDate": "2026-08-24 06:55:16 -0400",
    "value": "HKCategoryValueSleepAnalysisAsleepREM",
}

# The device string the export attaches to record types that do carry one.
DEVICE_STRING = (
    "<<HKDevice: 0x66aaba640>, name:Apple Watch, manufacturer:Apple Inc., "
    "model:Watch, hardware:Watch6,12, software:26.2>"
)


class TestSourceNameIsReadWhenNoDeviceAttribute:
    def test_sleep_record_without_a_device_attribute_keeps_its_source_name(self, service: XMLService) -> None:
        """The whole point: a watch's night must arrive named."""
        record = service._normalize_sleep_record(dict(WATCH_SLEEP_RECORD))

        assert record is not None
        assert record.source is not None
        assert record.source.name == "Apple Watch Ultra 3 Current"

    def test_the_device_string_still_wins_when_present(self, service: XMLService) -> None:
        """sourceName is a fallback, not an override — the device names the recorder."""
        info = service._extract_device_info(DEVICE_STRING, "Michael's iPhone")

        assert info.name == "Apple Watch"
        assert info.device_hardware_version == "Watch6,12"

    def test_source_name_fills_in_when_the_device_string_names_nothing(self, service: XMLService) -> None:
        info = service._extract_device_info("<<HKDevice: 0x1>, manufacturer:Apple Inc.>", "Oura")

        assert info.name == "Oura"

    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_a_blank_source_name_stays_none_rather_than_becoming_a_source(
        self, service: XMLService, empty: str | None
    ) -> None:
        """An empty string would create a data source named "" — worse than unknown."""
        assert service._extract_device_info("", empty).name is None

    def test_no_device_and_no_source_name_is_still_tolerated(self, service: XMLService) -> None:
        record = service._normalize_sleep_record({**WATCH_SLEEP_RECORD, "sourceName": None})

        assert record is not None
        assert record.source is not None
        assert record.source.name is None

    def test_two_watches_in_one_export_stay_distinguishable(self, service: XMLService) -> None:
        """The reason this matters: an export spans every watch the user has paired.

        Collapsing them to one nameless source makes a replaced watch's history
        indistinguishable from the current one's.
        """
        names = {
            service._normalize_sleep_record({**WATCH_SLEEP_RECORD, "sourceName": name}).source.name  # ty:ignore[possibly-unbound-attribute]
            for name in ("Michael's Apple Watch", "Apple Watch Ultra 3 Current")
        }

        assert names == {"Michael's Apple Watch", "Apple Watch Ultra 3 Current"}

    def test_an_unreadable_stage_is_still_rejected(self, service: XMLService) -> None:
        """Naming the source must not smuggle through a record we cannot read."""
        assert service._normalize_sleep_record({**WATCH_SLEEP_RECORD, "value": "banana"}) is None
