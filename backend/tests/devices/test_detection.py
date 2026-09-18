"""Detection must over-split rather than over-merge.

A wrong split is visible and reversible: an extra device appears and someone merges
it. A wrong merge silently pools two units' samples into one stream, produces no
symptom, and may not surface until the data is in an analysis - at which point the
samples cannot be separated again. These tests pin that asymmetry.
"""

from datetime import UTC, datetime
from typing import Never
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, Device, DeviceIdentity, User
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import DeviceIdentityKind, IdentityConfidence, ProviderName
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


class TestAggregatorRelaysAreNotPooled:
    """The model string on an aggregator route names the phone, not the recorder.

    HealthKit reports ``productType``, so a Muse headband, an Oura ring and a WHOOP
    band relayed through one iPhone all arrive stamped "iPhone15,3". Grouping on that
    pooled every app behind one phone into a single device - a silent over-merge, and
    the one this class pins shut.
    """

    def test_two_writers_behind_one_phone_are_two_devices(self, db: Session, user: User) -> None:
        muse = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        oura = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Oura")

        assert muse.device_id is not None
        assert oura.device_id is not None
        assert muse.device_id != oura.device_id
        assert len(_devices(db, user)) == 2

    def test_one_writer_across_two_phones_over_splits(self, db: Session, user: User) -> None:
        """Deliberately two devices, not one.

        The same headband synced by an old phone and a new one shares no identifier,
        and the pair cannot tell "one unit, two handsets" from "two units". A person
        merges them; nothing was pooled while they decided.
        """
        old_phone = _ensure(db, user, ProviderName.APPLE, "iPhone10,5", "Muse")
        new_phone = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")

        assert old_phone.device_id != new_phone.device_id

    def test_the_same_writer_and_phone_stay_one_device(self, db: Session, user: User) -> None:
        """Re-syncing must be idempotent - the pair is a stable key, not a new one."""
        first = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        again = DataSourceRepository().ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.APPLE,
            device_model="iPhone15,3",
            source="Muse",
        )
        assert first.id == again.id
        assert len(_devices(db, user)) == 1

    def test_an_app_writing_under_a_watch_splits_off_the_watch(self, db: Session, user: User) -> None:
        """SleepWatch derives sleep from an Apple Watch; it is not the watch.

        Keeping them on one device would let a watch-vs-headband comparison silently
        include a third party's derived staging.
        """
        watch = _ensure(db, user, ProviderName.APPLE, "Watch7,5", "Michael's Apple Watch")
        app = _ensure(db, user, ProviderName.APPLE, "Watch7,5", "SleepWatch")

        assert watch.device_id != app.device_id

    def test_a_relayed_device_is_named_after_its_writer(self, db: Session, user: User) -> None:
        """Display must not call a Muse headband "iPhone 14 Pro Max"."""
        muse = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        device = DeviceRepository().get(db, muse.device_id)

        assert device.label == "Muse"
        # The provider's own string is still recorded verbatim; it just does not name
        # the device on screen.
        assert device.model_raw == "iPhone15,3"

    def test_a_direct_route_still_groups_on_the_model_alone(self, db: Session, user: User) -> None:
        """Nothing changes where the model really does name the recorder."""
        activities = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        wellness = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "connect")

        assert activities.device_id == wellness.device_id

    def test_the_xml_import_literal_is_not_treated_as_a_writer(self, db: Session, user: User) -> None:
        """`apple_health_xml` is the importer's stamp, identical on every row it writes.

        Read as a writer id it gave every XML-imported source the same identity, so the
        first device claimed it and every later one collided with that claim.
        """
        watch = _ensure(db, user, ProviderName.APPLE, "Watch7,5", "apple_health_xml")
        phone = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "apple_health_xml")

        assert watch.device_id != phone.device_id
        values = {claim.id_value for claim in db.query(DeviceIdentity).filter(DeviceIdentity.user_id == user.id).all()}
        assert "apple_health_xml" not in values


class TestDeliberateDetachment:
    def test_a_locked_source_is_not_re_attached(self, db: Session, user: User) -> None:
        """Unlinking by hand has to outlive the next sync.

        Attribution is write-once for a source that has a device, but a NULL device_id
        otherwise reads as "never attributed" - so detection re-attached what someone
        had just removed, and the detach lasted until the following batch.
        """
        source = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        assert source.device_id is not None

        source.device_id = None
        source.attribution_locked_at = datetime.now(UTC)
        db.flush()

        resolved = DeviceDetectionService().resolve_for_data_source(db, source)

        assert resolved is None
        assert source.device_id is None

    def test_clearing_the_lock_lets_detection_speak_again(self, db: Session, user: User) -> None:
        source = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "Muse")
        source.device_id = None
        source.attribution_locked_at = None
        db.flush()

        resolved = DeviceDetectionService().resolve_for_data_source(db, source)

        assert resolved is not None
        assert source.device_id == resolved.id
