"""The per-account connection endpoints.

The provider-scoped routes still mean "this provider, all of it", which is the
right reading of "disconnect me from Whoop". These are what let an operator name
one of several accounts: relabel it, correct the e-mail that records where its
data came from, or drop it without touching the device worn beside it.
"""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.schemas.auth import ConnectionStatus
from tests.factories import UserConnectionFactory, UserFactory


class TestListing:
    def test_several_accounts_with_one_provider_are_all_listed_and_positioned(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left", account_label="P01 left wrist")
        UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right", account_label="P01 right wrist")
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header)
        assert response.status_code == 200

        garmin = [c for c in response.json() if c["provider"] == "garmin"]
        assert len(garmin) == 2
        assert {c["account_index"] for c in garmin} == {1, 2}
        assert all(c["account_count"] == 2 for c in garmin)
        assert {c["display_label"] for c in garmin} == {"P01 left wrist", "P01 right wrist"}

    def test_an_unnamed_account_still_has_a_display_label(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        UserConnectionFactory(user=user, provider="polar", provider_user_id="p-1", provider_username="athlete7")
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header)
        assert response.json()[0]["display_label"] == "athlete7"


class TestReadOne:
    def test_one_account_by_id(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(
            user=user, provider="whoop", provider_user_id="w-1", account_email="p01@lab.example.edu"
        )
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections/accounts/{connection.id}", headers=api_key_header)
        assert response.status_code == 200
        assert response.json()["account_email"] == "p01@lab.example.edu"

    def test_an_account_belonging_to_another_user_is_not_found(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """A connection id must not be a way to read across participants."""
        other = UserFactory()
        theirs = UserConnectionFactory(user=other, provider="whoop", provider_user_id="w-other")
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections/accounts/{theirs.id}", headers=api_key_header)
        assert response.status_code == 404

    def test_an_unknown_account_is_not_found(
        self, client: TestClient, user: User, api_key_header: dict[str, str]
    ) -> None:
        response = client.get(f"/api/v1/users/{user.id}/connections/accounts/{uuid4()}", headers=api_key_header)
        assert response.status_code == 404


class TestUpdate:
    def test_renaming_an_account_leaves_its_email_alone(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """A partial update must not drop the provenance record."""
        connection = UserConnectionFactory(
            user=user, provider="whoop", provider_user_id="w-1", account_email="p01@lab.example.edu"
        )
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_label": "P01 chest"},
            headers=api_key_header,
        )
        assert response.status_code == 200
        assert response.json()["account_label"] == "P01 chest"
        assert response.json()["account_email"] == "p01@lab.example.edu"

    def test_an_explicit_null_clears_a_field(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1", account_label="old")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_label": None},
            headers=api_key_header,
        )
        assert response.status_code == 200
        assert response.json()["account_label"] is None

    def test_an_email_already_used_by_a_sibling_account_is_rejected(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """Two accounts claiming one login would make the e-mail useless as provenance."""
        UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1", account_email="p01@lab.example.edu")
        second = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-2")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{second.id}",
            json={"account_email": "p01@lab.example.edu"},
            headers=api_key_header,
        )
        assert response.status_code == 409

    def test_a_malformed_email_is_rejected(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_email": "not-an-email"},
            headers=api_key_header,
        )
        assert response.status_code == 422

    def test_updating_another_users_account_is_not_found(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        other = UserFactory()
        theirs = UserConnectionFactory(user=other, provider="whoop", provider_user_id="w-other")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{theirs.id}",
            json={"account_label": "mine now"},
            headers=api_key_header,
        )
        assert response.status_code == 404


class TestDisconnectOne:
    def test_disconnecting_one_account_leaves_the_other_active(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right")
        db.commit()

        response = client.delete(f"/api/v1/users/{user.id}/connections/accounts/{left.id}", headers=api_key_header)
        assert response.status_code == 204

        db.refresh(left)
        db.refresh(right)
        assert left.status == ConnectionStatus.REVOKED
        assert right.status == ConnectionStatus.ACTIVE

    def test_the_provider_route_still_disconnects_every_account(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right")
        db.commit()

        response = client.delete(f"/api/v1/users/{user.id}/connections/garmin", headers=api_key_header)
        assert response.status_code == 204

        db.refresh(left)
        db.refresh(right)
        assert left.status == ConnectionStatus.REVOKED
        assert right.status == ConnectionStatus.REVOKED
