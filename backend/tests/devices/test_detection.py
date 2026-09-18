"""Detection must over-split rather than over-merge.

A wrong split is visible and reversible: an extra device appears and someone merges
it. A wrong merge silently pools two units' samples into one stream, produces no
symptom, and may not surface until the data is in an analysis - at which point the
samples cannot be separated again. These tests pin that asymmetry.
"""

from typing import Never
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, Device, DeviceIdentity, User
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import (
    DeviceIdentityKind,
    DeviceType,
    IdentityConfidence,
    LabelSource,
    ProviderName,
)
from app.services.devices.detection import DeviceDetectionService
from app.services.devices.identity import IdentityClaim


@pytest.fixture
def user(db: Session) -> User:
    user = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    return user


def _ensure(
    db: Session,
    user: User,
    provider: ProviderName,
    device_model: str | None,
    source: str | None,
    claims: list[IdentityClaim] | None = None,
) -> DataSource:
    """Ingest a data source the way a provider does, claims and all."""
    return DataSourceRepository().ensure_data_source(
        db,
        user_id=user.id,
        provider=provider,
        device_model=device_model,
        source=source,
        identity_claims=claims,
    )


def _devices(db: Session, user: User) -> list[Device]:
    return DeviceRepository().list_for_user(db, user.id)


class TestWithinRouteGrouping:
    def test_same_model_on_one_route_is_one_device(self, db: Session, user: User) -> None:
        """Garmin's activities and its wellness stream are one watch, not two.

        This is the case the old model could not express: both rows carry "fenix 8"
        but differ in `source`, so they are two data sources by identity.
        """
        first = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        second = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "connect")

        assert first.device_id is not None
        assert first.device_id == second.device_id
        assert len(_devices(db, user)) == 1

    def test_different_models_on_one_route_stay_apart(self, db: Session, user: User) -> None:
        watch = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        scale = _ensure(db, user, ProviderName.GARMIN, "Index S2", "garmin")
        assert watch.device_id != scale.device_id
        assert len(_devices(db, user)) == 2

    def test_source_without_a_model_is_left_unattributed(self, db: Session, user: User) -> None:
        """Whoop, Oura's data endpoints and Garmin's wellness summaries report no device.

        Unattributed is a normal resting state. Minting a device from an empty signal
        would put a row in the registry that looks like evidence of hardware.
        """
        source = _ensure(db, user, ProviderName.WHOOP, None, "whoop")
        assert source.device_id is None
        assert _devices(db, user) == []


class TestCrossRouteIsolation:
    def test_same_model_on_two_routes_stays_two_devices(self, db: Session, user: User) -> None:
        """The core rule. One ring, two routes, no shared identifier - so two devices.

        Linking them is an inference from evidence that a person confirms. Doing it
        here would be a guess that silently pools two streams which legitimately
        differ in freshness, rounding and completeness.
        """
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        via_apple = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")

        assert direct.device_id is not None
        assert via_apple.device_id is not None
        assert direct.device_id != via_apple.device_id
        assert len(_devices(db, user)) == 2


class TestStrongClaims:
    def test_a_strong_claim_groups_across_model_strings(self, db: Session, user: User) -> None:
        """A provider-issued id outranks the model string.

        A firmware update that changes how the model is spelled must not mint a
        second device when the provider is still naming the same unit.
        """
        claim = IdentityClaim(
            route=ProviderName.APPLE.value,
            kind=DeviceIdentityKind.HEALTHKIT_DEVICE_ID,
            value="A1B2C3D4-0000-1111-2222-333344445555",
            confidence=IdentityConfidence.STRONG,
        )

        first = _ensure(db, user, ProviderName.APPLE, "Watch7,5", "Ali's Watch", claims=[claim])
        second = _ensure(db, user, ProviderName.APPLE, "Watch7,9", "Ali's Watch renamed", claims=[claim])

        assert first.device_id is not None
        assert first.device_id == second.device_id

    def test_a_weak_claim_alone_never_creates_a_device(self, db: Session, user: User) -> None:
        """An app bundle id names a writer, not a unit."""
        source = _ensure(db, user, ProviderName.APPLE, None, "com.ouraring.oura")
        assert source.device_id is None

    def test_conflicting_strong_claims_produce_a_proposal_not_a_merge(self, db: Session, user: User) -> None:
        """A claim that has moved is either a re-paired device or a second unit.

        The data cannot tell those apart, so nothing is repointed and the pair goes
        to a human.
        """
        repo = DeviceRepository()
        detector = DeviceDetectionService(repo)

        shared = IdentityClaim(
            route=ProviderName.APPLE.value,
            kind=DeviceIdentityKind.HEALTHKIT_DEVICE_ID,
            value="SHARED-DEVICE-ID",
            confidence=IdentityConfidence.STRONG,
        )

        # Two devices already exist, and the claim is attached to the first.
        first = _ensure(db, user, ProviderName.APPLE, "Watch7,5", "watch-a", claims=[shared])
        second = _ensure(db, user, ProviderName.APPLE, "Watch6,2", "watch-b")

        assert first.device_id != second.device_id
        original_owner = first.device_id

        # The same strong claim now arrives on the second device's source.
        detector.resolve_for_data_source(db, second, extra_claims=[shared])

        # The claim did not move, and the two devices are still two.
        owner = repo.find_by_claim(db, user.id, shared)
        assert owner is not None
        assert owner.id == original_owner
        assert second.device_id != first.device_id

        proposals = repo.pending_proposals(db, user.id)
        assert len(proposals) == 1
        assert {proposals[0].device_a_id, proposals[0].device_b_id} == {first.device_id, second.device_id}


class TestIdempotence:
    def test_re_ingesting_the_same_source_does_not_duplicate(self, db: Session, user: User) -> None:
        for _ in range(3):
            _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")

        assert len(_devices(db, user)) == 1
        claims = db.query(DeviceIdentity).filter(DeviceIdentity.user_id == user.id).all()
        assert len(claims) == 1

    def test_attribution_failure_does_not_lose_the_data_source(
        self, db: Session, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attribution is enrichment. A failure must cost the device, never the data."""
        import app.services.devices.detection as detection

        def boom(*_args: object, **_kwargs: object) -> Never:
            raise RuntimeError("detection exploded")

        monkeypatch.setattr(detection.DeviceDetectionService, "resolve_for_data_source", boom)

        source = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        assert source is not None
        assert db.get(DataSource, source.id) is not None
        assert source.device_id is None


class TestRelayedThroughAPhone:
    """A third-party app relaying through HealthKit reports the phone that ran it.

    Every app on one handset then reports the same model string, so grouping on it
    pooled unrelated brands into a single device named after the phone - the exact
    over-merge this module exists to prevent, reached through the rule meant to
    prevent it. These pin the writer-keyed grouping that replaced it.
    """

    def test_two_apps_on_one_phone_are_two_devices(self, db: Session, user: User) -> None:
        muse = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")
        oura = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Oura")

        assert muse.device_id is not None
        assert oura.device_id is not None
        assert muse.device_id != oura.device_id
        assert len(_devices(db, user)) == 2

    def test_the_phone_is_kept_as_provenance_not_as_the_model(self, db: Session, user: User) -> None:
        """model_raw asserts what the provider said this unit was. It said nothing."""
        source = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")
        device = DeviceRepository().get(db, source.device_id)

        assert device.model_raw is None
        assert device.model_display is None
        assert device.host_model_raw == "iPhone 17 Pro"
        # The data source itself is untouched: it is the ingest fingerprint.
        assert source.device_model == "iPhone 17 Pro"

    def test_no_claim_describes_the_phone(self, db: Session, user: User) -> None:
        """A host claim on one writer's device collides with the next writer's sync."""
        _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")
        values = {c.id_value for c in db.query(DeviceIdentity).filter(DeviceIdentity.user_id == user.id).all()}
        assert values == {"Muse"}

    def test_the_writing_app_names_and_types_the_device(self, db: Session, user: User) -> None:
        """A starting point, not an answer - but a better one than "Apple phone"."""
        source = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")
        device = DeviceRepository().get(db, source.device_id)

        assert device.brand == "Muse"
        assert device.device_type == DeviceType.EEG.value
        assert device.label == "Muse"
        # Auto, so a hand-set name replaces it and then pins the device.
        assert device.label_source == LabelSource.AUTO.value

    def test_an_unrecognised_writer_gets_no_brand_rather_than_the_platform_s(self, db: Session, user: User) -> None:
        """Naming a headband "Apple" is worse than leaving the brand for a person."""
        source = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Elite HRV")
        device = DeviceRepository().get(db, source.device_id)

        assert device.brand is None
        assert device.device_type == DeviceType.UNKNOWN.value

    def test_apple_s_own_phone_data_still_describes_the_phone(self, db: Session, user: User) -> None:
        """The platform writing about its own handset genuinely means the handset."""
        source = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Michael's iPhone")
        device = DeviceRepository().get(db, source.device_id)

        assert device.model_raw == "iPhone 17 Pro"
        assert device.host_model_raw is None

    def test_a_watch_relayed_by_its_own_app_keeps_its_model(self, db: Session, user: User) -> None:
        """Only a model naming a phone is suspect; real hardware still groups on it."""
        first = _ensure(db, user, ProviderName.APPLE, "Apple Watch Ultra 3 49mm", "Ali's Watch")
        second = _ensure(db, user, ProviderName.APPLE, "Apple Watch Ultra 3 49mm", "Ali's Watch (2)")

        assert first.device_id == second.device_id
        device = DeviceRepository().get(db, first.device_id)
        assert device.model_raw == "Apple Watch Ultra 3 49mm"

    def test_re_ingesting_a_relayed_source_is_idempotent(self, db: Session, user: User) -> None:
        for _ in range(3):
            _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")

        assert len(_devices(db, user)) == 1
        assert DeviceRepository().pending_proposals(db, user.id) == []

    def test_health_connect_packages_split_the_same_way(self, db: Session, user: User) -> None:
        fitbit = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.fitbit.FitbitMobile")
        whoop = _ensure(db, user, ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.whoop.android")

        assert fitbit.device_id != whoop.device_id
