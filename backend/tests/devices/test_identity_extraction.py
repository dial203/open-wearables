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
