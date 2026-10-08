"""Tests for the one-off data migration that relabels Apple Watch sleeping wrist temperature
body_temperature -> skin_temperature.

The SDK metric map filed HKQuantityTypeIdentifierAppleSleepingWristTemperature under
body_temperature (id=45). A stored row keeps no HealthKit type, so the script finds wrist
temperature by its data source: an Apple source whose provider-reported hardware is an Apple
Watch. Thermometer and manual body temperature, which arrive from a phone or name no hardware,
must stay. See scripts/data_migrations/relabel_apple_wrist_temp_to_skin_temp.py.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataPointSeriesArchive, DataSource, SeriesTypeDefinition
from app.schemas.enums.aggregation_method import AggregationMethod
from app.schemas.enums.provider import ProviderName
from tests.factories import DataPointSeriesFactory, DataSourceFactory

BODY_TEMP_ID = 45
SKIN_TEMP_ID = 46
HEART_RATE_ID = 1

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "relabel_apple_wrist_temp_to_skin_temp.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("relabel_apple_wrist_temp_to_skin_temp", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


relabel_apple_wrist_temp = _load_module().relabel_apple_wrist_temp

NIGHT = datetime(2026, 9, 20, 3, 41, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _series_type(db: Session, type_id: int) -> SeriesTypeDefinition:
    """Fetch a series type seeded at session scope (see conftest.engine)."""
    return db.get(SeriesTypeDefinition, type_id)


def _source(
    *,
    provider: ProviderName = ProviderName.APPLE,
    device_model: str | None = "Watch7,5",
    source: str = "Test User's Apple Watch",
    device_model_origin: str | None = "provider",
) -> DataSource:
    """A data source as the SDK writes one; the defaults are an Apple Watch writing its own data."""
    return DataSourceFactory(
        provider=provider,
        device_model=device_model,
        device_model_origin=device_model_origin if device_model is not None else None,
        source=source,
        original_source_name=source,
    )


def _point(
    db: Session,
    source: DataSource,
    *,
    type_id: int = BODY_TEMP_ID,
    recorded_at: datetime = NIGHT,
    value: str = "35.12",
) -> DataPointSeries:
    return DataPointSeriesFactory(
        data_source=source,
        series_type=_series_type(db, type_id),
        recorded_at=recorded_at,
        value=Decimal(value),
    )


def _archive_row(db: Session, source: DataSource, *, type_id: int, bucket_start_at: datetime) -> None:
    db.add(
        DataPointSeriesArchive(
            id=uuid4(),
            data_source_id=source.id,
            series_type_definition_id=type_id,
            bucket_start_at=bucket_start_at,
            aggregation_type=AggregationMethod.AVG,
            value=Decimal("35.1"),
            sample_count=1,
        )
    )
    db.flush()


def _type_id(db: Session, point: DataPointSeries) -> int:
    return db.execute(
        text("SELECT series_type_definition_id FROM data_point_series WHERE id = :id"), {"id": point.id}
    ).scalar_one()


def _type_ids(db: Session, table: str) -> list[int]:
    rows = db.execute(text(f"SELECT series_type_definition_id FROM {table}")).scalars().all()
    return sorted(rows)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("device_model", "source"),
    [
        ("Watch7,5", "Test User's Apple Watch"),  # SDK: HKSourceRevision.productType
        ("Watch", "apple_health_xml"),  # XML import before sources were keyed on the writer
        ("Watch", "Test User's Apple Watch"),  # XML import keyed on sourceName
    ],
)
def test_relabels_body_temp_written_by_an_apple_watch(db: Session, device_model: str, source: str) -> None:
    point = _point(db, _source(device_model=device_model, source=source))

    result = relabel_apple_wrist_temp(db, dry_run=False)

    assert result["series_updated"] == 1
    assert _type_id(db, point) == SKIN_TEMP_ID


def test_leaves_thermometer_and_manual_body_temp_untouched(db: Session) -> None:
    """Core readings arrive from a phone, from hardware that is not a watch, or name none."""
    watch = _point(db, _source())
    phone = _point(db, _source(device_model="iPhone15,2", source="Thermometer"), value="36.8")
    no_hardware = _point(db, _source(device_model=None, source="Health"), value="36.9")
    thermometer = _point(db, _source(device_model="Kinsa QuickCare", source="Kinsa"), value="37.1")

    result = relabel_apple_wrist_temp(db, dry_run=False)

    assert result["series_updated"] == 1
    assert _type_id(db, watch) == SKIN_TEMP_ID
    assert [_type_id(db, p) for p in (phone, no_hardware, thermometer)] == [BODY_TEMP_ID] * 3


def test_leaves_a_watch_model_filled_in_from_a_device_label_untouched(db: Session) -> None:
    """Only a model the payload named is evidence of what wrote the row."""
    point = _point(db, _source(device_model_origin="label"))

    result = relabel_apple_wrist_temp(db, dry_run=False)

    assert result["series_updated"] == 0
    assert _type_id(db, point) == BODY_TEMP_ID


def test_leaves_other_providers_untouched(db: Session) -> None:
    point = _point(db, _source(provider=ProviderName.ULTRAHUMAN, device_model="Watch7,5"))

    result = relabel_apple_wrist_temp(db, dry_run=False)

    assert result["series_updated"] == 0
    assert _type_id(db, point) == BODY_TEMP_ID


def test_leaves_the_watch_s_other_series_untouched(db: Session) -> None:
    watch = _source()
    _point(db, watch)
    heart_rate = _point(db, watch, type_id=HEART_RATE_ID, value="58")

    relabel_apple_wrist_temp(db, dry_run=False)

    assert _type_id(db, heart_rate) == HEART_RATE_ID


def test_dry_run_makes_no_changes_and_reports_what_it_matched(db: Session, capsys: pytest.CaptureFixture[str]) -> None:
    _point(db, _source())
    _point(db, _source(device_model="Watch", source="apple_health_xml"), recorded_at=NIGHT - timedelta(days=1))
    _point(db, _source(device_model="iPhone15,2", source="Thermometer"), value="36.8")

    result = relabel_apple_wrist_temp(db, dry_run=True)

    assert result["series_updated"] == 2  # reported as "would update"
    assert _type_ids(db, "data_point_series") == [BODY_TEMP_ID] * 3
    out = capsys.readouterr().out
    assert "on sources not named as an Apple Watch (review): 1" in out
    assert "Apple body_temperature rows left as they are:   1" in out


def test_idempotent_second_run_is_noop(db: Session) -> None:
    _point(db, _source())

    relabel_apple_wrist_temp(db, dry_run=False)
    second = relabel_apple_wrist_temp(db, dry_run=False)

    assert second["series_updated"] == 0
    assert second["series_deleted"] == 0
    assert _type_ids(db, "data_point_series") == [SKIN_TEMP_ID]


def test_handles_unique_conflict_with_existing_skin_temp(db: Session) -> None:
    """If the same HealthKit sample was re-synced after the mapping change, a correct
    skin_temperature row already sits at the same (source, recorded_at) - the stale
    body_temperature duplicate must be removed instead of violating the unique constraint."""
    watch = _source()
    _point(db, watch, type_id=BODY_TEMP_ID)
    _point(db, watch, type_id=SKIN_TEMP_ID)

    result = relabel_apple_wrist_temp(db, dry_run=False)

    assert result["series_deleted"] == 1
    assert result["series_updated"] == 0
    rows = db.query(DataPointSeries).filter(DataPointSeries.data_source_id == watch.id).all()
    assert len(rows) == 1
    assert rows[0].series_type_definition_id == SKIN_TEMP_ID


def test_relabels_archive_table(db: Session) -> None:
    day = datetime(2026, 9, 20, tzinfo=timezone.utc)
    _archive_row(db, _source(), type_id=BODY_TEMP_ID, bucket_start_at=day)
    _archive_row(
        db, _source(device_model="iPhone15,2", source="Thermometer"), type_id=BODY_TEMP_ID, bucket_start_at=day
    )

    result = relabel_apple_wrist_temp(db, dry_run=False)

    assert result["archive_updated"] == 1
    assert _type_ids(db, "data_point_series_archive") == [BODY_TEMP_ID, SKIN_TEMP_ID]


def test_handles_unique_conflict_in_archive_table(db: Session) -> None:
    day = datetime(2026, 9, 20, tzinfo=timezone.utc)
    watch = _source()
    _archive_row(db, watch, type_id=BODY_TEMP_ID, bucket_start_at=day)
    _archive_row(db, watch, type_id=SKIN_TEMP_ID, bucket_start_at=day)

    result = relabel_apple_wrist_temp(db, dry_run=False)

    assert result["archive_deleted"] == 1
    assert result["archive_updated"] == 0
    assert _type_ids(db, "data_point_series_archive") == [SKIN_TEMP_ID]
