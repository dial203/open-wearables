"""The device registry's hand-editing surface, end to end.

Auto-detection deliberately leaves the ambiguous cases alone, so these endpoints are
not a fallback - they are how the ambiguous cases get resolved. What they must
guarantee is that no edit is silent and no edit crosses a user boundary.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataSource, User
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import DeviceType, ProviderName
from tests.factories import UserFactory


@pytest.fixture
def other_user(db: Session) -> User:
    return UserFactory()


def _ensure(db: Session, user: User, provider: ProviderName, model: str | None, source: str | None) -> DataSource:
    data_source = DataSourceRepository().ensure_data_source(
        db, user_id=user.id, provider=provider, device_model=model, source=source
    )
    db.commit()
    return data_source


class TestListAndGet:
    def test_detected_devices_are_listed_with_their_sources_and_claims(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        _ensure(db, user, ProviderName.GARMIN, "fenix 8", "connect")

        response = client.get(f"/api/v1/users/{user.id}/devices", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()

        assert body["total"] == 1
        device = body["items"][0]
        assert device["model_raw"] == "fenix 8"
        assert device["label_source"] == "auto"
        # Attribution groups the two sources; it does not merge them.
        assert len(device["data_sources"]) == 2
        assert {c["id_kind"] for c in device["identities"]} == {"model_string"}

    def test_another_users_device_reads_as_not_found(
        self, client: TestClient, db: Session, user: User, other_user: User, auth_headers: dict[str, str]
    ) -> None:
        """Not-found rather than forbidden: a distinct error confirms the id exists."""
        theirs = _ensure(db, other_user, ProviderName.GARMIN, "fenix 8", "garmin")

        response = client.get(
            f"/api/v1/users/{user.id}/devices/{theirs.device_id}",
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_unknown_device_is_not_found(self, client: TestClient, user: User, auth_headers: dict[str, str]) -> None:
        response = client.get(f"/api/v1/users/{user.id}/devices/{uuid4()}", headers=auth_headers)
        assert response.status_code == 404

    def test_authentication_is_required(self, client: TestClient, user: User) -> None:
        assert client.get(f"/api/v1/users/{user.id}/devices").status_code == 401


class TestEditing:
    def test_creating_by_hand_marks_the_label_manual(
        self, client: TestClient, user: User, auth_headers: dict[str, str]
    ) -> None:
        response = client.post(
            f"/api/v1/users/{user.id}/devices",
            headers=auth_headers,
            json={
                "device_type": DeviceType.CHEST_STRAP.value,
                "brand": "Polar",
                "model_raw": "H10",
                "label": "reference strap",
                "reason": "criterion device for the validation arm",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["label"] == "reference strap"
        assert body["label_source"] == "manual"

    def test_editing_records_who_and_why(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        source = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")

        response = client.patch(
            f"/api/v1/users/{user.id}/devices/{source.device_id}",
            headers=auth_headers,
            json={"label": "Sub 04 fenix", "wear_location": "left wrist", "reason": "study labelling"},
        )
        assert response.status_code == 200
        assert response.json()["label_source"] == "manual"

        history = client.get(
            f"/api/v1/users/{user.id}/devices-history",
            headers=auth_headers,
            params={"device_id": str(source.device_id)},
        ).json()
        updates = [e for e in history["items"] if e["action"] == "updated"]
        assert {e["field"] for e in updates} == {"label", "wear_location"}
        assert all(e["actor"] == "test@example.com" for e in updates)
        assert all(e["reason"] == "study labelling" for e in updates)

    def test_provider_reported_fields_are_rejected(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        """model_raw and brand are the record of what the provider claimed."""
        source = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        response = client.patch(
            f"/api/v1/users/{user.id}/devices/{source.device_id}",
            headers=auth_headers,
            json={"model_raw": "something else"},
        )
        # Not part of the schema at all, so it is simply not applied.
        assert response.status_code == 200
        assert response.json()["model_raw"] == "fenix 8"

    def test_retiring_records_the_effective_date(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        source = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")

        response = client.post(
            f"/api/v1/users/{user.id}/devices/{source.device_id}/retire",
            headers=auth_headers,
            json={"retired": True, "effective_at": "2026-06-01T00:00:00Z", "reason": "warranty replacement"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["is_active"] is False
        assert body["retired_at"].startswith("2026-06-01")

        active_only = client.get(
            f"/api/v1/users/{user.id}/devices",
            headers=auth_headers,
            params={"include_retired": False},
        ).json()
        assert active_only["total"] == 0


class TestLinking:
    def test_linking_a_source_moves_it_and_is_recorded(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        """The hand fix for the cross-route case detection refuses to guess at."""
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        relayed = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        assert direct.device_id != relayed.device_id

        response = client.post(
            f"/api/v1/users/{user.id}/devices/{direct.device_id}/link",
            headers=auth_headers,
            json={"data_source_id": str(relayed.id), "reason": "same ring, two routes"},
        )
        assert response.status_code == 200
        assert len(response.json()["data_sources"]) == 2

        # The trail keeps both attributions: detection's original one and this move.
        # That is the point of an append-only log - "which device did this source
        # belong to in March" has to stay answerable after someone re-links it.
        history = client.get(f"/api/v1/users/{user.id}/devices-history", headers=auth_headers).json()
        linked = [e for e in history["items"] if e["action"] == "linked" and e["data_source_id"] == str(relayed.id)]
        assert len(linked) == 2

        # Newest first, so the hand-made link leads and carries the operator's reason.
        assert linked[0]["reason"] == "same ring, two routes"
        assert linked[0]["actor"] == "test@example.com"
        assert linked[0]["new_value"] == str(direct.device_id)
        assert linked[1]["actor"] == "system:detection"

    def test_unlinking_a_foreign_source_is_refused(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        garmin = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        oura = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")

        response = client.post(
            f"/api/v1/users/{user.id}/devices/{garmin.device_id}/unlink",
            headers=auth_headers,
            json={"data_source_id": str(oura.id)},
        )
        assert response.status_code == 400


class TestMergeAndSplit:
    def test_merge_then_split_round_trips_the_attribution(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        """Split is the undo for a merge, which is the only irreversible operation."""
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        relayed = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        keep_id, absorbed_id = direct.device_id, relayed.device_id

        merged = client.post(
            f"/api/v1/users/{user.id}/devices/{keep_id}/merge",
            headers=auth_headers,
            json={"absorb_device_id": str(absorbed_id), "reason": "confirmed by session overlap"},
        )
        assert merged.status_code == 200
        assert len(merged.json()["data_sources"]) == 2
        assert client.get(f"/api/v1/users/{user.id}/devices/{absorbed_id}", headers=auth_headers).status_code == 404

        split = client.post(
            f"/api/v1/users/{user.id}/devices/{keep_id}/split",
            headers=auth_headers,
            json={"data_source_ids": [str(relayed.id)], "reason": "merge was wrong"},
        )
        assert split.status_code == 200
        new_device = split.json()
        assert [ds["id"] for ds in new_device["data_sources"]] == [str(relayed.id)]

        history = client.get(f"/api/v1/users/{user.id}/devices-history", headers=auth_headers).json()
        actions = {e["action"] for e in history["items"]}
        assert {"merged", "split"} <= actions

    def test_merge_history_survives_the_deleted_device(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        """The absorbed row is gone, so everything to rebuild it must be in the entry."""
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        relayed = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        absorbed_id = relayed.device_id

        client.post(
            f"/api/v1/users/{user.id}/devices/{direct.device_id}/merge",
            headers=auth_headers,
            json={"absorb_device_id": str(absorbed_id)},
        )

        history = client.get(f"/api/v1/users/{user.id}/devices-history", headers=auth_headers).json()
        entry = next(e for e in history["items"] if e["action"] == "merged")
        assert entry["meta"]["absorbed"]["id"] == str(absorbed_id)
        assert entry["meta"]["absorbed"]["model_raw"] == "Oura Ring Gen3"
        assert str(relayed.id) in entry["meta"]["moved_data_source_ids"]

    def test_splitting_everything_off_is_refused(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        only = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        response = client.post(
            f"/api/v1/users/{user.id}/devices/{only.device_id}/split",
            headers=auth_headers,
            json={"data_source_ids": [str(only.id)]},
        )
        assert response.status_code == 400


class TestProposals:
    def test_rejecting_a_proposal_is_permanent(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        relayed = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")

        # Detection surfaces the conflicting pair; here we seed one directly so the
        # test does not depend on session data.
        from app.repositories.device_repository import DeviceRepository

        repo = DeviceRepository()
        repo.upsert_proposal(db, user.id, direct.device_id, relayed.device_id, score=92.0)
        db.commit()

        listed = client.get(f"/api/v1/users/{user.id}/device-link-proposals", headers=auth_headers).json()
        assert listed["total"] == 1
        proposal_id = listed["items"][0]["id"]

        decided = client.post(
            f"/api/v1/users/{user.id}/device-link-proposals/{proposal_id}/decide",
            headers=auth_headers,
            json={"accepted": False, "reason": "two rings, alternated"},
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "rejected"

        refreshed = client.post(f"/api/v1/users/{user.id}/device-link-proposals/refresh", headers=auth_headers).json()
        assert refreshed["total"] == 0

    def test_accepting_a_proposal_merges(
        self, client: TestClient, db: Session, user: User, auth_headers: dict[str, str]
    ) -> None:
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        relayed = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")

        from app.repositories.device_repository import DeviceRepository

        repo = DeviceRepository()
        proposal = repo.upsert_proposal(db, user.id, direct.device_id, relayed.device_id, score=95.0)
        db.commit()

        response = client.post(
            f"/api/v1/users/{user.id}/device-link-proposals/{proposal.id}/decide",
            headers=auth_headers,
            json={"accepted": True},
        )
        assert response.status_code == 200

        remaining = client.get(f"/api/v1/users/{user.id}/devices", headers=auth_headers).json()
        assert remaining["total"] == 1
        assert len(remaining["items"][0]["data_sources"]) == 2
