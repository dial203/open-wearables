"""Classifying a connected account, and telling two accounts apart downstream.

The e-mail says *which* account a data set came from. The classification says
what it is for, which is what a study filters on - the validation arm separated
from the participant's own everyday wear, the checkout account kept out of the
analysis. These cover the classification itself, the disambiguation it feeds in
the data-source listing, and the device labels derived from what providers
already send.
"""

from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import AccountType, ProviderName
from tests.factories import UserConnectionFactory


class TestClassifyingAnAccount:
    def test_an_account_can_be_classified(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-1")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_type": "validation"},
            headers=api_key_header,
        )
        assert response.status_code == 200
        assert response.json()["account_type"] == "validation"

    def test_the_gold_standard_rig_can_be_classified_as_the_reference(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """A consumer scores every validation arm against this account, so it has a word of its own."""
        connection = UserConnectionFactory(user=user, provider="polar", provider_user_id="p-1")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_type": AccountType.REFERENCE.value},
            headers=api_key_header,
        )
        assert response.status_code == 200
        assert response.json()["account_type"] == "reference"

        listed = client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header).json()
        assert [c["account_type"] for c in listed] == ["reference"]

    def test_an_unclassified_account_reports_as_null_rather_than_guessing(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """No default: a classification nobody chose could mislead an analysis."""
        UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-1")
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header)
        assert response.json()[0]["account_type"] is None

    def test_an_unknown_classification_is_rejected(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-1")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_type": "whatever-i-like"},
            headers=api_key_header,
        )
        assert response.status_code == 400

    def test_classification_survives_an_unrelated_edit(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id="g-1", account_type="validation"
        )
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_label": "P01 arm A"},
            headers=api_key_header,
        )
        assert response.json()["account_type"] == "validation"

    def test_an_explicit_null_unclassifies(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id="g-1", account_type="validation"
        )
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/connections/accounts/{connection.id}",
            json={"account_type": None},
            headers=api_key_header,
        )
        assert response.json()["account_type"] is None


class TestTellingSourcesApartDownstream:
    def _source(self, db: Session, user: User, connection_id: UUID, model: str) -> None:
        DataSourceRepository().ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.GARMIN,
            user_connection_id=connection_id,
            device_model=model,
        )
        db.commit()

    def test_two_accounts_on_one_model_get_distinguishable_names(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """ "Garmin - fenix 7" twice is what this exists to prevent."""
        left = UserConnectionFactory(
            user=user,
            provider="garmin",
            provider_user_id="g-left",
            account_type=AccountType.VALIDATION.value,
            account_label="P01 arm A",
        )
        right = UserConnectionFactory(
            user=user,
            provider="garmin",
            provider_user_id="g-right",
            account_type=AccountType.PERSONAL.value,
            account_label="P01 own watch",
        )
        db.commit()
        self._source(db, user, left.id, "fenix 7")
        self._source(db, user, right.id, "fenix 7")

        items = client.get(f"/api/v1/users/{user.id}/data-sources", headers=api_key_header).json()["items"]
        names = {item["display_name"] for item in items}

        assert len(names) == 2, f"two accounts collapsed onto one name: {names}"
        assert "Garmin - fenix 7 (validation · P01 arm A)" in names
        assert "Garmin - fenix 7 (personal · P01 own watch)" in names

    def test_a_single_account_name_is_unchanged(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """Existing consumers read this string; it must not move for them."""
        only = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-1", account_type="personal")
        db.commit()
        self._source(db, user, only.id, "fenix 7")

        items = client.get(f"/api/v1/users/{user.id}/data-sources", headers=api_key_header).json()["items"]
        assert items[0]["display_name"] == "Garmin - fenix 7"

    def test_the_listing_carries_the_classification(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id="g-1", account_type="validation"
        )
        db.commit()
        self._source(db, user, connection.id, "fenix 7")

        items = client.get(f"/api/v1/users/{user.id}/data-sources", headers=api_key_header).json()["items"]
        assert items[0]["account_type"] == "validation"
        assert items[0]["user_connection_id"] == str(connection.id)


class TestDevicesLabelThemselves:
    def test_the_model_a_provider_reports_is_surfaced_without_anyone_typing_it(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        connection = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-1")
        db.commit()
        DataSourceRepository().ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.GARMIN,
            user_connection_id=connection.id,
            device_model="fenix 7",
        )
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header)
        assert response.json()[0]["observed_devices"] == ["fenix 7"]

    def test_opaque_hardware_codes_are_humanised(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """ "Watch7,5" is not a device name anyone can read."""
        connection = UserConnectionFactory(user=user, provider="apple", provider_user_id=None)
        db.commit()
        DataSourceRepository().ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            user_connection_id=connection.id,
            device_model="Watch7,5",
        )
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header)
        assert response.json()[0]["observed_devices"] == ["Apple Watch Series 8"]

    def test_each_account_reports_only_its_own_devices(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right")
        db.commit()
        repo = DataSourceRepository()
        repo.ensure_data_source(
            db, user_id=user.id, provider=ProviderName.GARMIN, user_connection_id=left.id, device_model="fenix 7"
        )
        repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.GARMIN,
            user_connection_id=right.id,
            device_model="Forerunner 965",
        )
        db.commit()

        by_id = {c["id"]: c for c in client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header).json()}
        assert by_id[str(left.id)]["observed_devices"] == ["fenix 7"]
        assert by_id[str(right.id)]["observed_devices"] == ["Forerunner 965"]

    def test_a_provider_reporting_no_device_leaves_the_list_empty(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """Whoop exposes no device; that is what the manual label is for."""
        UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        db.commit()

        response = client.get(f"/api/v1/users/{user.id}/connections", headers=api_key_header)
        assert response.json()[0]["observed_devices"] == []
