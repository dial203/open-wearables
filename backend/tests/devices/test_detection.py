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

from app.models import DataSource, Device, DeviceIdentity, User, UserConnection
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
from tests.factories import UserConnectionFactory


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


def _connection(
    db: Session,
    user: User,
    provider: ProviderName,
    *,
    athlete_id: str,
    sensor_label: str | None = None,
) -> UserConnection:
    """One connected account, as a study running an account per wearable would have."""
    connection = UserConnectionFactory(
        user=user,
        provider=provider.value,
        provider_user_id=athlete_id,
        sensor_label=sensor_label,
    )
    db.flush()
    return connection


def _ensure_for(
    db: Session,
    user: User,
    provider: ProviderName,
    connection: UserConnection,
    device_model: str | None,
    source: str | None,
) -> DataSource:
    return DataSourceRepository().ensure_data_source(
        db,
        user_id=user.id,
        provider=provider,
        user_connection_id=connection.id,
        device_model=device_model,
        source=source,
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
        """A host claim on one writer's device collides with the next writer's sync.

        The grouping key names the host, but only ever bound to the writer that relayed
        through it, so it is a value no other app can produce. What must not exist is a
        claim on the bare host string, which every app on that phone would also make.
        """
        _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")
        values = {c.id_value for c in db.query(DeviceIdentity).filter(DeviceIdentity.user_id == user.id).all()}

        assert "iPhone 17 Pro" not in values
        assert values == {"Muse", "Muse||iPhone 17 Pro"}

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


class TestOneWriterAcrossTwoHosts:
    """The grouping key is the writer *and* the host, not the writer alone.

    Keyed on the writer alone, one app's streams group across every handset it ever
    synced through: an Oura app relaying through a 2017 phone and a 2024 phone reads
    as one ring, and for someone who replaced the ring in between that is two units
    merged with no symptom. The pair over-splits instead, which a person can undo.
    """

    def test_the_same_app_on_two_phones_is_two_devices(self, db: Session, user: User) -> None:
        old_phone = _ensure(db, user, ProviderName.APPLE, "iPhone 12 Mini", "Oura")
        new_phone = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Oura")

        assert old_phone.device_id != new_phone.device_id

    def test_the_same_app_on_one_phone_stays_one_device(self, db: Session, user: User) -> None:
        """Re-syncing must be idempotent: the pair is a stable key, not a new one."""
        first = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")
        again = _ensure(db, user, ProviderName.APPLE, "iPhone 17 Pro", "Muse")

        assert first.id == again.id
        assert len(_devices(db, user)) == 1

    def test_the_xml_import_literal_is_not_treated_as_a_writer(self, db: Session, user: User) -> None:
        """`apple_health_xml` is the importer's stamp, identical on every row it writes.

        Read as a writer id it gave every XML-imported source the same identity, so the
        first device claimed it and every later one collided with that claim.
        """
        watch = _ensure(db, user, ProviderName.APPLE, "Watch7,5", "apple_health_xml")
        phone = _ensure(db, user, ProviderName.APPLE, "iPhone15,3", "apple_health_xml")

        assert watch.device_id != phone.device_id
        values = {c.id_value for c in db.query(DeviceIdentity).filter(DeviceIdentity.user_id == user.id).all()}
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


class TestAccountScopedGrouping:
    """Several Strava accounts under one user, which is how a validation arm is run.

    Grouping on the bare model string reproduces what a provider asserts, and that
    holds while a user has one account. With several it silently pools two units of
    the same model, which is the merge this module exists to prevent.
    """

    def test_two_accounts_with_the_same_model_stay_apart(self, db: Session, user: User) -> None:
        left = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_1")
        right = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_2")

        first = _ensure_for(db, user, ProviderName.STRAVA, left, "Apple Watch Series 9", "Strava App")
        second = _ensure_for(db, user, ProviderName.STRAVA, right, "Apple Watch Series 9", "Strava App")

        assert first.device_id is not None
        assert second.device_id is not None
        assert first.device_id != second.device_id
        assert len(_devices(db, user)) == 2

    def test_one_account_still_groups_its_own_sources(self, db: Session, user: User) -> None:
        """Over-splitting per account must not become over-splitting per sync."""
        connection = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_1")

        first = _ensure_for(db, user, ProviderName.STRAVA, connection, "COROS PACE 3", "COROS")
        second = _ensure_for(db, user, ProviderName.STRAVA, connection, "COROS PACE 3", "File upload")

        assert first.device_id == second.device_id
        assert len(_devices(db, user)) == 1

    def test_other_routes_keep_grouping_across_accounts(self, db: Session, user: User) -> None:
        left = _connection(db, user, ProviderName.GARMIN, athlete_id="garmin_1")
        right = _connection(db, user, ProviderName.GARMIN, athlete_id="garmin_2")

        first = _ensure_for(db, user, ProviderName.GARMIN, left, "fenix 8", "garmin")
        second = _ensure_for(db, user, ProviderName.GARMIN, right, "fenix 8", "garmin")

        assert first.device_id == second.device_id


class TestDeclaredSensor:
    """The reference instrument no provider names.

    Strava reports the watch that uploaded; a chest strap paired to it appears in no
    field, and an activity with heart rate looks the same either way. Declaring it on
    the connection is the only way the reference is nameable at all.
    """

    def test_the_strap_becomes_the_device_and_the_watch_its_host(self, db: Session, user: User) -> None:
        connection = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_1", sensor_label="Polar H10")

        source = _ensure_for(db, user, ProviderName.STRAVA, connection, "Garmin Forerunner 965", "Garmin Connect")

        device = DeviceRepository().get(db, source.device_id)
        assert device is not None
        assert device.host_model_raw == "Garmin Forerunner 965"
        # Never written where it would read as this unit's own model.
        assert device.model_raw is None
        assert device.label == "Polar H10"
        assert device.device_type == DeviceType.CHEST_STRAP

    def test_the_providers_report_survives_on_the_data_source(self, db: Session, user: User) -> None:
        """A declaration reinterprets the report; it must not erase it."""
        connection = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_1", sensor_label="Polar H10")

        source = _ensure_for(db, user, ProviderName.STRAVA, connection, "Garmin Forerunner 965", "Garmin Connect")

        assert source.device_model == "Garmin Forerunner 965"

    def test_the_source_is_typed_by_the_sensor_not_the_recorder(self, db: Session, user: User) -> None:
        """device_type ranks sources against each other, so the wrong one demotes the reference."""
        connection = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_1", sensor_label="Polar H10")

        source = _ensure_for(db, user, ProviderName.STRAVA, connection, "Garmin Forerunner 965", "Garmin Connect")

        assert source.device_type == DeviceType.CHEST_STRAP

    def test_an_account_without_a_declaration_is_unaffected(self, db: Session, user: User) -> None:
        connection = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_2")

        source = _ensure_for(db, user, ProviderName.STRAVA, connection, "Garmin Forerunner 965", "Garmin Connect")

        device = DeviceRepository().get(db, source.device_id)
        assert device is not None
        assert device.model_raw == "Garmin Forerunner 965"
        assert device.host_model_raw is None

    def test_one_account_declared_does_not_reclassify_another(self, db: Session, user: User) -> None:
        """The gold-standard account and a validation account, same watch model."""
        reference = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_1", sensor_label="Polar H10")
        validation = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_2")

        gold = _ensure_for(db, user, ProviderName.STRAVA, reference, "Garmin Forerunner 965", "Garmin Connect")
        other = _ensure_for(db, user, ProviderName.STRAVA, validation, "Garmin Forerunner 965", "Garmin Connect")

        assert gold.device_id != other.device_id
        assert gold.device_type == DeviceType.CHEST_STRAP
        assert other.device_type == DeviceType.WATCH

    def test_a_declaration_the_type_tables_do_not_recognise_falls_through(self, db: Session, user: User) -> None:
        """A free-text note must not flatten a perfectly good model string to unknown."""
        connection = _connection(db, user, ProviderName.STRAVA, athlete_id="athlete_3", sensor_label="reference strap")

        source = _ensure_for(db, user, ProviderName.STRAVA, connection, "Garmin Forerunner 965", "Garmin Connect")

        assert source.device_type == DeviceType.WATCH
