"""Tests for the startup backfill that re-resolves NULL/'other' data_source.device_type."""

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy.orm import Session

from app.schemas.enums import ProviderName
from tests.factories import DataSourceFactory, UserConnectionFactory, UserFactory

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "data_migrations" / "backfill_device_types.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backfill_device_types", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backfill_device_types = _load_module().backfill_device_types


class TestBackfillDeviceTypes:
    def test_upgrades_null_and_other_rows(self, db: Session) -> None:
        watch = DataSourceFactory(
            provider=ProviderName.SAMSUNG, device_model="SM-L315F", source="Galaxy Watch7", device_type="other"
        )
        whoop = DataSourceFactory(provider=ProviderName.WHOOP, device_model=None, source="whoop", device_type=None)

        backfill_device_types(db, dry_run=False)

        assert watch.device_type == "watch"
        assert whoop.device_type == "band"

    def test_recomputes_cloud_provider_types(self, db: Session) -> None:
        bpm = DataSourceFactory(
            provider=ProviderName.GARMIN, device_model="Garmin Index BPM", source="garmin", device_type="scale"
        )
        gearless = DataSourceFactory(
            provider=ProviderName.SUUNTO, device_model=None, source="suunto", device_type="watch"
        )

        backfill_device_types(db, dry_run=False)

        assert bpm.device_type == "bp_monitor"
        # Fork: a source with no model keeps a known type. The fork classifies from the
        # source label when the model is missing ("suunto" names a watch maker), and a
        # resolution that finds nothing never erases a type already stored - upstream
        # would clear this row to NULL.
        assert gearless.device_type == "watch"

    def test_keeps_a_declared_sensor(self, db: Session) -> None:
        """A strap declared on the connection still decides the type, as in live sync."""
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider=ProviderName.STRAVA.value, sensor_label="Polar H10")
        strap = DataSourceFactory(
            user=user,
            user_connection_id=connection.id,
            provider=ProviderName.STRAVA,
            device_model="Garmin Forerunner 965",
            source="strava",
            device_type="chest_strap",
        )

        backfill_device_types(db, dry_run=False)

        assert strap.device_type == "chest_strap"

    def test_never_overwrites_concrete_sdk_type(self, db: Session) -> None:
        ds = DataSourceFactory(
            provider=ProviderName.APPLE, device_model="Watch6,12", source="AirPods Pro", device_type="phone"
        )

        backfill_device_types(db, dry_run=False)

        assert ds.device_type == "phone"

    def test_dry_run_changes_nothing(self, db: Session) -> None:
        ds = DataSourceFactory(provider=ProviderName.WHOOP, device_model=None, source="whoop", device_type=None)

        changes = backfill_device_types(db, dry_run=True)

        assert changes["whoop: None -> band"] == 1
        assert ds.device_type is None

    def test_moves_ipad_phone_rows_to_tablet(self, db: Session) -> None:
        ipad = DataSourceFactory(
            provider=ProviderName.APPLE, device_model="iPad13,1", source="iPad", device_type="phone"
        )
        iphone = DataSourceFactory(
            provider=ProviderName.APPLE, device_model="iPhone15,2", source="iPhone", device_type="phone"
        )

        backfill_device_types(db, dry_run=False)

        assert ipad.device_type == "tablet"
        assert iphone.device_type == "phone"
