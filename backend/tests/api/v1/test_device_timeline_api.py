"""The dated device history endpoints on one connected account."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User, UserConnection
from app.schemas.enums import DeviceModelOrigin, ProviderName
from tests.factories import DataSourceFactory, EventRecordFactory, UserConnectionFactory, UserFactory

SWITCH = "2026-09-22T12:00:00+00:00"


def _url(user: User, connection: UserConnection, suffix: str = "") -> str:
    return f"/api/v1/users/{user.id}/connections/accounts/{connection.id}/device-timeline{suffix}"


def _put(client: TestClient, user: User, connection: UserConnection, headers: dict[str, str], periods: list) -> object:
    return client.put(_url(user, connection), json={"periods": periods}, headers=headers)


class TestTimeline:
    def test_an_account_without_one_reads_as_empty(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin", device_label="fenix 8")
        db.commit()

        body = client.get(_url(user, garmin), headers=api_key_header).json()

        assert body["periods"] == []
        assert body["device_label"] == "fenix 8"

    def test_stating_one_orders_it_and_moves_the_label_to_the_latest(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin", device_label="fenix 8")
        db.commit()

        response = _put(
            client,
            user,
            garmin,
            api_key_header,
            [
                {"device_label": " Venu X1 ", "effective_from": SWITCH},
                {"device_label": "fenix 8", "effective_from": None},
            ],
        )

        assert response.status_code == 200
        body = response.json()
        assert [p["device_label"] for p in body["periods"]] == ["fenix 8", "Venu X1"]
        assert body["periods"][0]["effective_to"] == body["periods"][1]["effective_from"]
        assert body["device_label"] == "Venu X1"
        account = client.get(f"/api/v1/users/{user.id}/connections/accounts/{garmin.id}", headers=api_key_header)
        assert account.json()["device_label"] == "Venu X1"

    def test_inconsistent_periods_are_rejected(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin")
        db.commit()

        for periods in (
            [{"device_label": "a", "effective_from": None}, {"device_label": "b", "effective_from": None}],
            [{"device_label": "a", "effective_from": SWITCH}, {"device_label": "b", "effective_from": SWITCH}],
            [{"device_label": "   ", "effective_from": SWITCH}],
            # A wall-clock time with no offset names no instant.
            [{"device_label": "a", "effective_from": "2026-09-22T12:00:00"}],
        ):
            response = _put(client, user, garmin, api_key_header, periods)
            assert response.status_code in (400, 422), periods

    def test_an_undated_label_edit_is_refused_while_a_timeline_exists(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin")
        db.commit()
        _put(client, user, garmin, api_key_header, [{"device_label": "Venu X1", "effective_from": None}])
        account = f"/api/v1/users/{user.id}/connections/accounts/{garmin.id}"

        refused = client.patch(account, json={"device_label": "fenix 8"}, headers=api_key_header)
        assert refused.status_code == 409

        # Resending the label it already has, or editing something else, is fine.
        assert client.patch(account, json={"device_label": "Venu X1"}, headers=api_key_header).status_code == 200
        assert client.patch(account, json={"account_label": "P01"}, headers=api_key_header).status_code == 200

        legacy = client.put(
            f"/api/v1/users/{user.id}/connections/garmin/device-label",
            params={"connection_id": str(garmin.id)},
            json={"device_label": "fenix 8"},
            headers=api_key_header,
        )
        assert legacy.status_code == 409

    def test_removing_it_frees_the_label_again(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin")
        db.commit()
        _put(client, user, garmin, api_key_header, [{"device_label": "Venu X1", "effective_from": None}])
        _put(client, user, garmin, api_key_header, [])

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{garmin.id}",
            json={"device_label": "fenix 8"},
            headers=api_key_header,
        )
        assert response.status_code == 200

    def test_another_users_account_is_not_found(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        theirs = UserConnectionFactory(user=UserFactory(), provider="garmin")
        db.commit()

        assert client.get(_url(user, theirs), headers=api_key_header).status_code == 404
        assert _put(client, user, theirs, api_key_header, []).status_code == 404
        assert client.post(_url(user, theirs, "/refile"), json={}, headers=api_key_header).status_code == 404


class TestRefileEndpoint:
    def test_it_is_a_dry_run_unless_told_otherwise(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin")
        db.commit()
        start = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=1)
        _put(client, user, garmin, api_key_header, [{"device_label": "Venu X1", "effective_from": start.isoformat()}])

        response = client.post(_url(user, garmin, "/refile"), json={}, headers=api_key_header)

        assert response.status_code == 200
        assert response.json()["dry_run"] is True

    def test_an_account_without_a_timeline_is_a_bad_request(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin")
        db.commit()

        response = client.post(_url(user, garmin, "/refile"), json={"dry_run": False}, headers=api_key_header)

        assert response.status_code == 400


class TestResendingTheCurrentLabel:
    def test_it_does_not_stamp_todays_label_onto_an_unstated_stretch(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """A model-less source on a dated account is unattributed on purpose."""
        from app.models import DataSource
        from app.schemas.enums import ProviderName
        from tests.factories import DataSourceFactory

        garmin = UserConnectionFactory(user=user, provider="garmin")
        unstated = DataSourceFactory(
            user=user, provider=ProviderName.GARMIN, user_connection_id=garmin.id, device_model=None, source="garmin"
        )
        db.commit()
        _put(client, user, garmin, api_key_header, [{"device_label": "Venu X1", "effective_from": SWITCH}])

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{garmin.id}",
            json={"device_label": "Venu X1", "account_label": "personal"},
            headers=api_key_header,
        )

        assert response.status_code == 200
        db.expire_all()
        assert db.get(DataSource, unstated.id).device_model is None


class TestDetect:
    def test_detecting_dates_the_watches_the_workouts_name(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        garmin = UserConnectionFactory(user=user, provider="garmin", device_label="Venu X1")
        march = datetime(2026, 3, 1, 7, tzinfo=timezone.utc)
        june = datetime(2026, 6, 1, 7, tzinfo=timezone.utc)
        for model, at in (("fenix 7", march), ("Venu X1", june)):
            source = DataSourceFactory(
                user=user,
                provider=ProviderName.GARMIN,
                user_connection_id=garmin.id,
                device_model=model,
                source="garmin",
                device_model_origin=DeviceModelOrigin.PROVIDER.value,
            )
            EventRecordFactory(data_source=source, start_datetime=at, end_datetime=at + timedelta(hours=1))
        db.commit()

        proposal = client.get(_url(user, garmin), headers=api_key_header).json()
        assert proposal["periods"] == []
        assert proposal["auto"] is True
        assert [p["device_label"] for p in proposal["detection"]["proposed"]] == ["fenix 7", "Venu X1"]

        response = client.post(_url(user, garmin, "/detect"), json={}, headers=api_key_header)

        assert response.status_code == 200
        body = response.json()
        assert body["changed"] is True
        periods = body["timeline"]["periods"]
        assert [(p["device_label"], p["origin"]) for p in periods] == [("fenix 7", "detected"), ("Venu X1", "detected")]
        assert periods[1]["previous_last_seen"] is not None

    def test_a_provider_that_names_no_device_is_a_bad_request(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        whoop = UserConnectionFactory(user=user, provider="whoop")
        db.commit()

        response = client.post(_url(user, whoop, "/detect"), json={}, headers=api_key_header)

        assert response.status_code == 400
