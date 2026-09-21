"""The data-source listing says which sources relay a brand that also arrives directly.

The rule that hides those copies is covered in tests/services/test_relay_dedup.py.
These cover what an operator sees and can change: the listing flags the relayed copy
and names the provider that made it redundant, and the override sticks.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from tests.factories import DataSourceFactory


def _sources(client: TestClient, user: User, api_key_header: dict[str, str]) -> dict[str, dict]:
    items = client.get(f"/api/v1/users/{user.id}/data-sources", headers=api_key_header).json()["items"]
    return {item["id"]: item for item in items}


class TestTheListing:
    def test_a_relayed_copy_is_flagged_and_names_its_direct_route(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        direct = DataSourceFactory(
            user=user, provider="oura", device_model="Oura Ring Gen3", source="oura_api", original_source_name="Oura"
        )
        relayed = DataSourceFactory(
            user=user, provider="apple", device_model=None, source="com.ouraring.oura", original_source_name="Oura"
        )
        db.commit()

        items = _sources(client, user, api_key_header)

        assert items[str(relayed.id)]["redundant_relay"] is True
        assert items[str(relayed.id)]["direct_provider"] == "oura"
        assert items[str(relayed.id)]["ingestion_route"] == "aggregator"
        assert items[str(direct.id)]["redundant_relay"] is False

    def test_a_source_with_no_direct_counterpart_is_not_flagged(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        relayed = DataSourceFactory(
            user=user, provider="apple", device_model=None, source="Zepp Life", original_source_name="Zepp"
        )
        db.commit()

        items = _sources(client, user, api_key_header)

        assert items[str(relayed.id)]["redundant_relay"] is False

    def test_every_source_reports_its_override(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        source = DataSourceFactory(user=user, provider="apple", source="com.apple.health.ABC")
        db.commit()

        items = _sources(client, user, api_key_header)

        assert items[str(source.id)]["relay_visibility"] == "auto"


class TestTheOverride:
    def test_a_source_can_be_kept_whatever_the_rule_says(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        source = DataSourceFactory(
            user=user, provider="apple", device_model=None, source="com.ouraring.oura", original_source_name="Oura"
        )
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/data-sources/{source.id}/relay-visibility",
            json={"relay_visibility": "always"},
            headers=api_key_header,
        )

        assert response.status_code == 200
        assert response.json()["relay_visibility"] == "always"
        assert _sources(client, user, api_key_header)[str(source.id)]["relay_visibility"] == "always"

    def test_an_unknown_setting_is_rejected(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """400, not 422: this API reports every validation failure as a bad request."""
        source = DataSourceFactory(user=user, provider="apple", source="com.apple.health.ABC")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/data-sources/{source.id}/relay-visibility",
            json={"relay_visibility": "sometimes"},
            headers=api_key_header,
        )

        assert response.status_code == 400

    def test_another_users_source_is_not_found(
        self, client: TestClient, db: Session, user: User, api_key_header: dict[str, str]
    ) -> None:
        """A source id is not a capability: answering would say whether it exists."""
        other_source = DataSourceFactory(provider="apple", source="com.apple.health.ABC")
        db.commit()

        response = client.patch(
            f"/api/v1/users/{user.id}/data-sources/{other_source.id}/relay-visibility",
            json={"relay_visibility": "never"},
            headers=api_key_header,
        )

        assert response.status_code == 404
