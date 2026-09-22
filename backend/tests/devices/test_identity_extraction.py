"""Identity claims are only as strong as the identifier behind them."""

from types import SimpleNamespace

from app.schemas.enums import DeviceIdentityKind, IdentityConfidence, ProviderName
from app.services.devices.identity import (
    IdentityClaim,
    claims_from_data_source,
    claims_from_garmin_summary,
    claims_from_oura_ring_config,
    claims_from_sdk_source,
    garmin_summary_prefix,
    relaying_host_model,
)


def _by_kind(claims: list[IdentityClaim], kind: DeviceIdentityKind) -> IdentityClaim | None:
    return next((c for c in claims if c.kind is kind), None)


class TestHealthKitSource:
    def test_device_id_is_strong_everything_else_is_weak(self) -> None:
        source = SimpleNamespace(
            device_id="A1B2C3D4-0000-1111-2222-333344445555",
            bundle_identifier="com.apple.health.ABCDEF",
            product_type="Watch7,5",
            device_model="Watch7,5",
            device_name="Ali's Apple Watch",
            app_id=None,
        )
        claims = claims_from_sdk_source(ProviderName.APPLE, source)

        device_id = _by_kind(claims, DeviceIdentityKind.HEALTHKIT_DEVICE_ID)
        assert device_id is not None
        assert device_id.confidence is IdentityConfidence.STRONG

        # A bundle id names the app, and two identical watches on one account share
        # it. Treating it as strong would merge them.
        bundle = _by_kind(claims, DeviceIdentityKind.HEALTHKIT_BUNDLE)
        assert bundle is not None
        assert bundle.confidence is IdentityConfidence.WEAK

        assert _by_kind(claims, DeviceIdentityKind.APPLE_PRODUCT_TYPE).confidence is IdentityConfidence.WEAK

    def test_pointer_address_from_xml_export_is_rejected(self) -> None:
        """Apple Health XML yields the HKDevice object's memory address, not an id.

        It differs on every export and collides across unrelated devices within one
        export, so as an identity it would both split one device across exports and
        merge different devices inside an export.
        """
        source = SimpleNamespace(
            device_id="0x280b6b840",
            bundle_identifier=None,
            product_type=None,
            device_model="Watch6,2",
            device_name=None,
            app_id=None,
        )
        claims = claims_from_sdk_source(ProviderName.APPLE, source)
        assert _by_kind(claims, DeviceIdentityKind.HEALTHKIT_DEVICE_ID) is None
        assert _by_kind(claims, DeviceIdentityKind.MODEL_STRING) is not None

    def test_third_party_writer_yields_only_its_package(self) -> None:
        """Third-party HealthKit writers pass no HKDevice, so there is no unit to name."""
        source = SimpleNamespace(
            device_id=None,
            bundle_identifier=None,
            app_id="com.ouraring.oura",
            product_type=None,
            device_model=None,
            device_name=None,
        )
        claims = claims_from_sdk_source(ProviderName.APPLE, source)
        assert [c.kind for c in claims] == [DeviceIdentityKind.HEALTHKIT_BUNDLE]
        assert claims[0].confidence is IdentityConfidence.WEAK

    def test_no_source_yields_nothing(self) -> None:
        assert claims_from_sdk_source(ProviderName.APPLE, None) == []


class TestOuraRingConfig:
    def test_config_id_is_the_strong_signal(self) -> None:
        claims = claims_from_oura_ring_config(
            {"id": "4f2a1c88-0000-0000-0000-000000000001", "hardware_type": "gen3", "design": "horizon"}
        )
        config_id = _by_kind(claims, DeviceIdentityKind.OURA_CONFIG_ID)
        assert config_id is not None
        assert config_id.confidence is IdentityConfidence.STRONG
        assert config_id.route == ProviderName.OURA.value

        # Every gen3 ring reports "gen3", so it cannot identify one.
        assert _by_kind(claims, DeviceIdentityKind.MODEL_STRING).confidence is IdentityConfidence.WEAK

    def test_missing_config_yields_nothing(self) -> None:
        assert claims_from_oura_ring_config(None) == []
        assert claims_from_oura_ring_config({}) == []


class TestGarminSummaryPrefix:
    def test_prefix_is_the_leading_token(self) -> None:
        assert garmin_summary_prefix("x153d4d8-5d5b43c0-900") == "x153d4d8"
        assert garmin_summary_prefix("x153d4d8-5d5b43c0") == "x153d4d8"

    def test_unshaped_ids_yield_nothing(self) -> None:
        assert garmin_summary_prefix("x153d4d8") is None
        assert garmin_summary_prefix("") is None
        assert garmin_summary_prefix(None) is None
        assert garmin_summary_prefix("-5d5b43c0") is None

    def test_prefix_enters_weak_until_validated(self) -> None:
        """Whether the prefix is per-device or per-account is unverified.

        Recording it weak means it accumulates for validation but cannot group
        anything, so being wrong costs nothing until someone checks.
        """
        claims = claims_from_garmin_summary("x153d4d8-5d5b43c0-900")
        assert len(claims) == 1
        assert claims[0].kind is DeviceIdentityKind.GARMIN_SUMMARY_PREFIX
        assert claims[0].confidence is IdentityConfidence.WEAK


class TestDataSourceBaseline:
    def test_model_string_is_claimed_for_every_provider(self) -> None:
        claims = claims_from_data_source(ProviderName.GARMIN, "fenix 8", "garmin")
        assert _by_kind(claims, DeviceIdentityKind.MODEL_STRING).value == "fenix 8"

    def test_android_routes_claim_the_writing_package(self) -> None:
        """Both Android routes carry a package name, so both claim it the same way.

        Upstream split the old `google` provider into GOOGLE_HEALTH (the cloud API)
        and HEALTH_CONNECT (on-device); the writer id means the same thing on each.
        """
        for route in (ProviderName.HEALTH_CONNECT, ProviderName.GOOGLE_HEALTH):
            claims = claims_from_data_source(route, None, "com.ouraring.oura")
            assert _by_kind(claims, DeviceIdentityKind.HEALTH_CONNECT_PACKAGE).value == "com.ouraring.oura"

    def test_direct_provider_source_literal_is_not_claimed(self) -> None:
        """For a direct API the `source` column is the provider's own literal.

        Claiming it would hand every user of that provider the same identity value,
        which on a shared route is the one thing that must not happen.
        """
        claims = claims_from_data_source(ProviderName.GARMIN, None, "garmin")
        assert claims == []

    def test_values_are_capped_to_the_column_width(self) -> None:
        claims = claims_from_data_source(ProviderName.GARMIN, "x" * 400, None)
        assert len(claims[0].value) == 255

    def test_blank_values_are_not_claims(self) -> None:
        assert claims_from_data_source(ProviderName.GARMIN, "   ", None) == []


class TestRelayingHost:
    """A model string that names the phone an app ran on is not about the device.

    Every app on one handset reports the same string, so treating it as this device's
    model groups unrelated brands together - the one over-merge a model string can
    cause. These pin where that is recognised and where it deliberately is not.
    """

    def test_a_third_party_app_on_a_phone_names_the_host(self) -> None:
        assert relaying_host_model(ProviderName.APPLE, "iPhone 17 Pro", "Muse") == "iPhone 17 Pro"
        assert (
            relaying_host_model(ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.fitbit.FitbitMobile") == "Pixel 9 Pro"
        )

    def test_the_platform_writing_about_its_own_handset_is_the_handset(self) -> None:
        assert relaying_host_model(ProviderName.APPLE, "iPhone 17 Pro", "com.apple.health") is None
        assert relaying_host_model(ProviderName.APPLE, "iPhone 17 Pro", "Michael's iPhone") is None
        assert relaying_host_model(ProviderName.HEALTH_CONNECT, "Pixel 9 Pro", "com.android.healthconnect") is None

    def test_the_platform_s_own_apps_are_the_handset(self) -> None:
        """HealthKit reports Apple's own apps by display name, not by bundle id.

        Their data was recorded by the phone or the watch, not relayed through it, so
        splitting them off would invent a nameless device for data the model string
        already describes correctly.
        """
        for writer in ("Health", "Fitness", "Clock", "cycle tracking"):
            assert relaying_host_model(ProviderName.APPLE, "iPhone 17 Pro", writer) is None

    def test_a_writer_that_is_another_name_for_the_handset_is_the_handset(self) -> None:
        """Platforms report a phone's own data under whatever the owner named it.

        Splitting these would turn one handset into several devices and throw its model
        away - strictly worse than leaving it alone.
        """
        assert relaying_host_model(ProviderName.SAMSUNG, "SM-S901U", "Michael's S22") is None
        assert relaying_host_model(ProviderName.SAMSUNG, "SM-G975U", "S10+") is None
        assert relaying_host_model(ProviderName.SAMSUNG, "LM-V350", "V35 ThinQ") is None

    def test_a_shared_brand_word_is_not_enough_to_be_the_handset(self) -> None:
        """ "Galaxy Watch" and "Galaxy S22" share a word and are two devices."""
        assert relaying_host_model(ProviderName.SAMSUNG, "SM-S901U", "Galaxy Watch7") == "SM-S901U"

    def test_a_wearable_relaying_through_a_phone_still_splits(self) -> None:
        """The watch is its own unit even when the phone is what synced its data."""
        assert relaying_host_model(ProviderName.APPLE, "iPhone10,5", "Ali's Apple Watch") == "iPhone10,5"

    def test_real_wearable_hardware_is_never_treated_as_a_host(self) -> None:
        assert relaying_host_model(ProviderName.APPLE, "Watch7,5", "Ali's Watch") is None
        assert relaying_host_model(ProviderName.APPLE, "Oura Ring Gen3", "Oura") is None

    def test_a_direct_route_is_never_treated_as_a_relay(self) -> None:
        """Only aggregators relay. A maker's own API reporting a phone means a phone."""
        assert relaying_host_model(ProviderName.GARMIN, "fenix 8", "garmin") is None

    def test_the_host_s_claims_are_not_emitted(self) -> None:
        """A host claim would be handed to whichever app synced first and collide after."""
        claims = claims_from_data_source(ProviderName.APPLE, "iPhone 17 Pro", "Muse")
        assert _by_kind(claims, DeviceIdentityKind.MODEL_STRING) is None
        assert _by_kind(claims, DeviceIdentityKind.HEALTHKIT_BUNDLE).value == "Muse"

    def test_the_sdk_path_suppresses_the_product_type_too(self) -> None:
        source = SimpleNamespace(
            device_id=None,
            bundle_identifier="com.interaxon.muse",
            product_type="iPhone18,1",
            device_model="iPhone 17 Pro",
            device_name=None,
            app_id=None,
        )
        claims = claims_from_sdk_source(ProviderName.APPLE, source)
        assert _by_kind(claims, DeviceIdentityKind.MODEL_STRING) is None
        assert _by_kind(claims, DeviceIdentityKind.APPLE_PRODUCT_TYPE) is None
        assert _by_kind(claims, DeviceIdentityKind.HEALTHKIT_BUNDLE).value == "com.interaxon.muse"


class TestAccountScopedModelStrings:
    """One user, several accounts with one provider: a shared model is not a shared unit.

    The within-route rule elsewhere - group on an exact model-string match -
    reproduces what a provider asserts, and that assertion holds for one account. It
    stops holding the moment a validation study runs an account per wearable: two
    devices of the same model then collapse into one row, with two units' data pooled
    into it and nothing on screen to say so.
    """

    def test_two_strava_accounts_do_not_share_a_model_key(self) -> None:
        first = claims_from_data_source(ProviderName.STRAVA, "Garmin Forerunner 965", "Garmin Connect", "athlete_1")
        second = claims_from_data_source(ProviderName.STRAVA, "Garmin Forerunner 965", "Garmin Connect", "athlete_2")

        assert (
            _by_kind(first, DeviceIdentityKind.MODEL_STRING).value
            != _by_kind(  # type: ignore[union-attr]
                second, DeviceIdentityKind.MODEL_STRING
            ).value
        )  # type: ignore[union-attr]

    def test_one_account_keys_consistently_across_syncs(self) -> None:
        first = claims_from_data_source(ProviderName.STRAVA, "Garmin Forerunner 965", "Garmin Connect", "athlete_1")
        again = claims_from_data_source(ProviderName.STRAVA, "Garmin Forerunner 965", "Strava App", "athlete_1")

        assert _by_kind(first, DeviceIdentityKind.MODEL_STRING) == _by_kind(again, DeviceIdentityKind.MODEL_STRING)

    def test_the_model_stays_legible_in_the_key(self) -> None:
        claim = _by_kind(
            claims_from_data_source(ProviderName.STRAVA, "COROS PACE 3", "COROS", "athlete_9"),
            DeviceIdentityKind.MODEL_STRING,
        )

        assert claim is not None
        assert claim.value.startswith("COROS PACE 3")

    def test_routes_that_are_not_account_scoped_are_untouched(self) -> None:
        """Scoping every route would churn stored claim values for no gain."""
        first = claims_from_data_source(ProviderName.GARMIN, "fenix 8", "garmin", "athlete_1")
        second = claims_from_data_source(ProviderName.GARMIN, "fenix 8", "garmin", "athlete_2")

        assert _by_kind(first, DeviceIdentityKind.MODEL_STRING) == _by_kind(second, DeviceIdentityKind.MODEL_STRING)

    def test_no_scope_behaves_as_before(self) -> None:
        claim = _by_kind(
            claims_from_data_source(ProviderName.STRAVA, "COROS PACE 3", "COROS"),
            DeviceIdentityKind.MODEL_STRING,
        )

        assert claim is not None
        assert claim.value == "COROS PACE 3"


class TestDeclaredSensor:
    """A chest strap recorded through a watch, which no provider reports.

    Strava names the device that uploaded and nothing about the strap paired to it;
    an activity with heart rate looks identical whichever sensor produced it. So the
    sensor is declared on the connection, and a declaration is not second-guessed.
    """

    def test_the_reported_model_becomes_the_recorder(self) -> None:
        assert (
            relaying_host_model(ProviderName.STRAVA, "Garmin Forerunner 965", "Polar H10", True)
            == "Garmin Forerunner 965"
        )

    def test_without_a_declaration_strava_keeps_its_model(self) -> None:
        """The relay inference is for phones on aggregator SDK routes, not for this."""
        assert relaying_host_model(ProviderName.STRAVA, "Garmin Forerunner 965", "Polar H10") is None

    def test_a_declaration_needs_something_to_have_been_reported(self) -> None:
        assert relaying_host_model(ProviderName.STRAVA, None, "Polar H10", True) is None

    def test_the_grouping_key_pairs_sensor_with_recorder(self) -> None:
        claims = claims_from_data_source(
            ProviderName.STRAVA, "Garmin Forerunner 965", "Garmin Connect", "athlete_1", "Polar H10"
        )
        claim = _by_kind(claims, DeviceIdentityKind.AGGREGATOR_WRITER_MODEL)

        assert claim is not None
        assert claim.value.startswith("Polar H10||Garmin Forerunner 965")

    def test_a_sensor_with_no_recorder_is_simply_the_unit(self) -> None:
        """Nothing was reported to be the recorder, so there is no host to record."""
        claims = claims_from_data_source(ProviderName.STRAVA, None, "strava", "athlete_1", "Polar H10")
        claim = _by_kind(claims, DeviceIdentityKind.MODEL_STRING)

        assert claim is not None
        assert claim.value.startswith("Polar H10")

    def test_the_declaration_replaces_the_writer_rather_than_joining_it(self) -> None:
        """Two writers on one row would key the same unit two ways across syncs."""
        claims = claims_from_data_source(ProviderName.APPLE, "iPhone15,3", "com.ouraring.oura", None, "Polar H10")

        assert _by_kind(claims, DeviceIdentityKind.HEALTHKIT_BUNDLE) is None
        claim = _by_kind(claims, DeviceIdentityKind.AGGREGATOR_WRITER_MODEL)
        assert claim is not None
        assert claim.value == "Polar H10||iPhone15,3"

    def test_every_claim_stays_weak(self) -> None:
        claims = claims_from_data_source(
            ProviderName.STRAVA, "Garmin Forerunner 965", "Garmin Connect", "athlete_1", "Polar H10"
        )

        assert all(c.confidence is IdentityConfidence.WEAK for c in claims)
