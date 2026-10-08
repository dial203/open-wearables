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
from uuid import uuid4

import pytest

from app.schemas.enums import SampleRoute
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.providers.apple.apple_xml.xml_service import XMLService

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


class TestTheImportDeclaresTheRealProvider:
    """An export and an SDK upload are the same Apple Health data by different routes."""

    def test_sleep_is_imported_as_apple_not_as_the_transport(self, service: XMLService) -> None:
        from app.schemas.enums.provider import ProviderName

        request = service._wrap_sleep_data([])

        assert request.provider == ProviderName.APPLE.value

    def test_the_declared_provider_is_a_real_provider_name(self, service: XMLService) -> None:
        """The reason this bit: an unknown name degrades silently, it does not raise.

        ``_build_creation`` resolves the provider inside ``suppress(ValueError)``, so a
        value that is not a ``ProviderName`` leaves the data source on whatever the
        source string implied — ``unknown`` when the source is null, which is exactly
        the pairing an export's sleep records produced.
        """
        from app.schemas.enums.provider import ProviderName

        # Constructing it must not raise; that is what the old value did.
        assert ProviderName(service._wrap_sleep_data([]).provider) is ProviderName.APPLE


def _hr_record(source_name: str | None, value: str, device: str | None = None) -> dict:
    record = {
        "type": "HKQuantityTypeIdentifierHeartRate",
        "unit": "count/min",
        "startDate": "2026-10-05 22:44:10 -0400",
        "endDate": "2026-10-05 22:44:10 -0400",
        "value": value,
    }
    if source_name is not None:
        record["sourceName"] = source_name
    if device is not None:
        record["device"] = device
    return record


class TestSeriesRecordsAreKeyedOnTheirWriter:
    """Two apps writing heart rate at the same second are two sources, not one row."""

    def _sample(self, service: XMLService, record: dict) -> TimeSeriesSampleCreate:
        sample = service._create_record(dict(record), uuid4())
        assert sample is not None
        return sample

    def test_two_writers_that_name_no_device_stay_apart(self, service: XMLService) -> None:
        polar = self._sample(service, _hr_record("Polar Flow", "142"))
        oura = self._sample(service, _hr_record("Oura", "128"))

        assert (polar.source, oura.source) == ("Polar Flow", "Oura")
        assert polar.recorded_at == oura.recorded_at

    def test_a_writer_named_after_a_vendor_is_still_an_apple_health_row(self, service: XMLService) -> None:
        """ "Oura" is the writing app inside Apple Health, not Oura's own connection."""
        oura = self._sample(service, _hr_record("Oura", "128"))

        assert oura.provider == "apple"
        assert oura.route == SampleRoute.APPLE_XML_RECORDS

    def test_two_watches_of_one_model_are_told_apart_by_their_names(self, service: XMLService) -> None:
        left = self._sample(service, _hr_record("Watch Left", "140", DEVICE_STRING))
        right = self._sample(service, _hr_record("Watch Right", "141", DEVICE_STRING))

        assert left.device_model == right.device_model == "Watch"
        assert left.source != right.source

    def test_a_record_naming_no_writer_keeps_the_old_source(self, service: XMLService) -> None:
        assert self._sample(service, _hr_record(None, "70")).source == "apple_health_xml"
