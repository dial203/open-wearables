"""An account is either one declared device or files each record under its own.

A study runs a gold-standard Strava account, whose every workout was recorded with
an ECG strap, beside validation accounts that each carry whatever devices synced to
them. The first is one device whatever uploaded it; the others are aggregators. The
switch between the two is ``sensor_label``, and it has to reach the data already
stored, not only the next sync.
"""

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import DataSource, DeviceHistory, User, UserConnection
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import DeviceType, ProviderName
from tests.factories import UserConnectionFactory


def _strava_account(db: Session, user: User, athlete_id: str, sensor_label: str | None = None) -> UserConnection:
    connection = UserConnectionFactory(
        user=user, provider="strava", provider_user_id=athlete_id, sensor_label=sensor_label
    )
    db.flush()
    return connection


def _source(db: Session, user: User, connection: UserConnection, model: str, source: str) -> DataSource:
    return DataSourceRepository().ensure_data_source(
        db,
        user_id=user.id,
        provider=ProviderName.STRAVA,
        user_connection_id=connection.id,
        device_model=model,
        source=source,
    )


def _patch(client: TestClient, user: User, connection: UserConnection, headers: dict[str, str], body: dict) -> dict:
    response = client.patch(f"/api/v1/users/{user.id}/connections/accounts/{connection.id}", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _history_rows(db: Session, user: User) -> int:
    return db.scalar(select(func.count()).select_from(DeviceHistory).where(DeviceHistory.user_id == user.id)) or 0


class TestTheModeIsReported:
    def test_an_account_with_no_declared_device_is_per_record(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = _strava_account(db, user, "athlete_1")
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections/accounts/{connection.id}", headers=api_key_header)

        assert response.json()["device_attribution"] == "per_record"

    def test_naming_the_device_makes_it_single_device(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = _strava_account(db, user, "athlete_1")
        db.commit()

        body = _patch(client, user, connection, api_key_header, {"sensor_label": "Polar H10"})

        assert body["device_attribution"] == "single_device"
        assert body["sensor_label"] == "Polar H10"


class TestSwitchingAppliesToStoredData:
    def test_declaring_the_strap_refiles_every_recorder_under_one_device(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = _strava_account(db, user, "athlete_1")
        runs = _source(db, user, connection, "Garmin Forerunner 965", "Garmin Connect")
        rides = _source(db, user, connection, "Wahoo ELEMNT BOLT", "Wahoo")
        db.commit()
        assert runs.device_id != rides.device_id

        _patch(client, user, connection, api_key_header, {"sensor_label": "Polar H10"})
        db.refresh(runs)
        db.refresh(rides)

        assert runs.device_id is not None
        assert runs.device_id == rides.device_id
        strap = DeviceRepository().get(db, runs.device_id)
        assert strap is not None
        assert strap.label == "Polar H10"
        assert strap.device_type == DeviceType.CHEST_STRAP
        assert runs.device_type == DeviceType.CHEST_STRAP
        assert rides.device_type == DeviceType.CHEST_STRAP
        # The recorder each activity named is kept on its own data source.
        assert runs.device_model == "Garmin Forerunner 965"
        assert rides.device_model == "Wahoo ELEMNT BOLT"

    def test_withdrawing_it_puts_each_recorder_back_on_its_own_device(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = _strava_account(db, user, "athlete_1")
        runs = _source(db, user, connection, "Garmin Forerunner 965", "Garmin Connect")
        rides = _source(db, user, connection, "Wahoo ELEMNT BOLT", "Wahoo")
        db.commit()
        watch, head_unit = runs.device_id, rides.device_id

        _patch(client, user, connection, api_key_header, {"sensor_label": "Polar H10"})
        _patch(client, user, connection, api_key_header, {"sensor_label": None})
        db.refresh(runs)
        db.refresh(rides)

        # The devices they had before, not new ones.
        assert runs.device_id == watch
        assert rides.device_id == head_unit
        assert runs.device_type == DeviceType.WATCH

    def test_withdrawing_it_restores_the_device_ingest_chose(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """Not whatever the stored model string resolves to now.

        Ingest can attribute on more than the model string (a device id, a ring
        configuration), so re-resolving from the data source alone could land a
        source somewhere other than where it was.
        """
        connection = _strava_account(db, user, "athlete_1")
        runs = _source(db, user, connection, "Garmin Forerunner 965", "Garmin Connect")
        repo = DeviceRepository()
        chosen = repo.create(db, user_id=user.id, device_type=DeviceType.WATCH.value, detected=True)
        repo.attach_data_source(db, runs, chosen)
        db.commit()

        _patch(client, user, connection, api_key_header, {"sensor_label": "Polar H10"})
        _patch(client, user, connection, api_key_header, {"sensor_label": None})
        db.refresh(runs)

        assert runs.device_id == chosen.id

    def test_saving_the_same_declaration_again_writes_nothing(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = _strava_account(db, user, "athlete_1")
        _source(db, user, connection, "Garmin Forerunner 965", "Garmin Connect")
        db.commit()
        _patch(client, user, connection, api_key_header, {"sensor_label": "Polar H10"})
        before = _history_rows(db, user)

        _patch(client, user, connection, api_key_header, {"sensor_label": "Polar H10"})

        assert _history_rows(db, user) == before

    def test_a_device_a_person_named_keeps_its_sources(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """Their judgement about which unit this is outranks a bulk re-file."""
        connection = _strava_account(db, user, "athlete_1")
        runs = _source(db, user, connection, "Garmin Forerunner 965", "Garmin Connect")
        db.commit()
        watch = DeviceRepository().get(db, runs.device_id)
        assert watch is not None
        DeviceRepository().update_fields(db, watch, {"label": "P01 left wrist"}, actor="researcher@lab.example.edu")
        db.commit()

        _patch(client, user, connection, api_key_header, {"sensor_label": "Polar H10"})
        db.refresh(runs)

        assert runs.device_id == watch.id
        # The type still follows the declaration: it is what ranks the source.
        assert runs.device_type == DeviceType.CHEST_STRAP

    def test_other_accounts_are_not_touched(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        reference = _strava_account(db, user, "athlete_1")
        validation = _strava_account(db, user, "athlete_2")
        _source(db, user, reference, "Garmin Forerunner 965", "Garmin Connect")
        theirs = _source(db, user, validation, "Garmin Forerunner 965", "Garmin Connect")
        db.commit()
        device_before = theirs.device_id

        _patch(client, user, reference, api_key_header, {"sensor_label": "Polar H10"})
        db.refresh(theirs)

        assert theirs.device_id == device_before
        assert theirs.device_type == DeviceType.WATCH

    def test_an_edit_that_does_not_send_the_field_changes_nothing(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = _strava_account(db, user, "athlete_1", sensor_label="Polar H10")
        _source(db, user, connection, "Garmin Forerunner 965", "Garmin Connect")
        db.commit()
        before = _history_rows(db, user)

        body = _patch(client, user, connection, api_key_header, {"account_label": "Gold standard"})

        assert body["device_attribution"] == "single_device"
        assert _history_rows(db, user) == before


class TestDataSourcesCarryTheAccountsAttribution:
    """A consumer naming sources from /data-sources has to know which account is one device."""

    def test_a_single_device_accounts_sources_name_the_device(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        reference = _strava_account(db, user, "athlete_1", sensor_label="Polar H10")
        validation = _strava_account(db, user, "athlete_2")
        _source(db, user, reference, "Garmin Forerunner 965", "Garmin Connect")
        _source(db, user, validation, "COROS PACE 3", "COROS")
        db.commit()

        items = client.get(f"/api/v1/users/{user.id}/data-sources", headers=api_key_header).json()["items"]
        by_model = {item["device_model"]: item for item in items}

        gold = by_model["Garmin Forerunner 965"]
        assert gold["device_attribution"] == "single_device"
        assert gold["sensor_label"] == "Polar H10"
        other = by_model["COROS PACE 3"]
        assert other["device_attribution"] == "per_record"
        assert other["sensor_label"] is None
