"""Tests for Polar RR-interval CSV parsing and creator building (pure, no DB)."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.schemas.enums import SeriesType
from app.services.polar_rr_import_service import PolarRrImportError, PolarRrImportService

CSV = b"duration,offline\n858,false\n878,false\n6819,true\n701,false\n"


def test_parse_rr_csv_skips_header_and_reads_rows() -> None:
    rows = PolarRrImportService.parse_rr_csv(CSV)
    assert rows == [(858, False), (878, False), (6819, True), (701, False)]


def test_parse_rr_csv_rejects_empty() -> None:
    with pytest.raises(PolarRrImportError):
        PolarRrImportService.parse_rr_csv(b"duration,offline\n")


def test_build_creators_timestamps_and_offline_handling() -> None:
    rows = [(858, False), (878, False), (6819, True), (701, False)]
    user_id = uuid4()
    start = datetime(2026, 7, 22, 0, 20, 9, tzinfo=timezone.utc)

    creators = PolarRrImportService.build_creators(
        rows,
        user_id=user_id,
        start_datetime=start,
        source="polar",
        device_model="Polar Vantage",
        zone_offset="-05:00",
    )

    # offline dropout (6819) is skipped, so 3 beats stored
    assert len(creators) == 3
    assert all(c.series_type == SeriesType.rr_interval for c in creators)
    assert [int(c.value) for c in creators] == [858, 878, 701]

    # clock advances over ALL intervals incl. the offline gap:
    # beat1 @ +858ms, beat2 @ +1736ms, beat4 @ +858+878+6819+701 = +9256ms
    assert creators[0].recorded_at == start + timedelta(milliseconds=858)
    assert creators[1].recorded_at == start + timedelta(milliseconds=1736)
    assert creators[2].recorded_at == start + timedelta(milliseconds=9256)

    # attribution carried through
    assert creators[0].provider == "polar"
    assert creators[0].source == "polar"
    assert creators[0].device_model == "Polar Vantage"
    assert creators[0].zone_offset == "-05:00"
    assert creators[0].user_id == user_id


def test_build_creators_all_offline_yields_none() -> None:
    creators = PolarRrImportService.build_creators(
        [(6000, True), (5000, True)],
        user_id=uuid4(),
        start_datetime=datetime(2026, 7, 22, tzinfo=timezone.utc),
        source="polar",
        device_model=None,
        zone_offset=None,
    )
    assert creators == []
