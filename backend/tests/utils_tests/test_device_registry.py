"""Tests for canonical brand + human-model normalization (device_registry).

Covers the real-world source/model strings observed across ingest paths:
direct provider APIs, Apple HealthKit source names, and Google Health /
Health Connect Android package + device_model shapes.
"""

import pytest

from app.schemas.enums import ProviderName
from app.utils.device_registry import (
    humanize_device_model,
    registry_device_name,
    resolve_brand,
    source_device_name,
)

P = ProviderName


@pytest.mark.parametrize(
    ("provider", "device_model", "source", "expected"),
    [
        # Health Connect (Android SDK push): Android package identifies the brand
        (P.HEALTH_CONNECT, None, "com.ouraring.oura", "Oura"),
        (P.HEALTH_CONNECT, None, "com.whoop.android", "Whoop"),
        (P.HEALTH_CONNECT, None, "com.fitbit.FitbitMobile", "Fitbit"),
        (P.HEALTH_CONNECT, None, "com.garmin.android.apps.connectmobile", "Garmin"),
        (P.HEALTH_CONNECT, None, "com.sec.android.app.shealth", "Samsung Health"),
        (P.HEALTH_CONNECT, "SM-S901U", "com.android.healthconnect.phone.jdef455", "Health Connect"),
        # Google Health API 24/7 stream: source is the constant, brand comes from device_model
        (P.GOOGLE_HEALTH, "FITBIT", "google_health_api", "Fitbit"),
        (P.GOOGLE_HEALTH, "Versa 4", "google_health_api", "Fitbit"),
        (P.GOOGLE_HEALTH, "Google Pixel Watch 4 (45mm)", "google_health_api", "Google"),
        (P.GOOGLE_HEALTH, "HEALTH_CONNECT", "google_health_api", "Health Connect"),
        # Apple HealthKit: source app name identifies the underlying brand
        (P.APPLE, "iPhone10,5", "Oura", "Oura"),
        (P.APPLE, "iPhone18,1", "WHOOP", "Whoop"),
        (P.APPLE, "iPhone18,1", "Connect", "Garmin"),
        (P.APPLE, "iPhone15,3", "Polar Flow", "Polar"),
        # Apple native data -> provider fallback
        (P.APPLE, "Watch7,12", "Michael's Apple Watch", "Apple"),
        # device_model wins over a generic source literal (Strava re-export of Garmin)
        (P.STRAVA, "Garmin fenix 8", "strava", "Garmin"),
        # Direct provider APIs
        (P.OURA, None, "oura", "Oura"),
        (P.WHOOP, None, "whoop", "Whoop"),
        (P.SAMSUNG, "SM-Q501", "Galaxy Ring", "Samsung"),
    ],
)
def test_resolve_brand(
    provider: ProviderName,
    device_model: str | None,
    source: str | None,
    expected: str | None,
) -> None:
    assert resolve_brand(provider, device_model, source) == expected


def test_resolve_brand_unknown_provider_without_signal_returns_none() -> None:
    assert resolve_brand(P.UNKNOWN, None, None) is None


def test_humanize_device_model_maps_known_codes() -> None:
    assert humanize_device_model("iPhone10,5") == "iPhone 8 Plus"
    assert humanize_device_model("SM-S901U") == "Galaxy S22"


def test_humanize_device_model_unknown_returns_none() -> None:
    # Unknown codes fall back to the raw identifier (caller keeps device_model).
    assert humanize_device_model("XYZ-123") is None
    assert humanize_device_model(None) is None


def test_humanize_device_model_names_the_family_of_an_apple_code_the_table_lacks() -> None:
    # A watch released after the table was last updated is still certainly a watch.
    assert humanize_device_model("Watch99,9") == "Apple Watch (Watch99,9)"
    assert humanize_device_model("iPhone99,1") == "iPhone (iPhone99,1)"


def test_humanize_device_model_falls_back_to_the_full_registry() -> None:
    # A real current productType must not reach the UI as the raw "Watch7,9".
    assert humanize_device_model("Watch7,9") == "Apple Watch Series 10 46mm (GPS)"
    assert humanize_device_model("iPhone17,1") == "iPhone 16 Pro"


@pytest.mark.parametrize(
    ("code", "name"),
    [
        # A second, shorter table once named the Ultra 2 a Series 8 and was read first.
        ("Watch7,5", "Apple Watch Ultra 2"),
        ("Watch7,12", "Apple Watch Ultra 3 49mm"),
        ("Watch8,1", "Apple Watch Ultra 4 49mm"),
        ("Watch8,2", "Apple Watch Series 12 42mm (GPS)"),
        ("Watch8,5", "Apple Watch Series 12 46mm (GPS+Cellular)"),
    ],
)
def test_humanize_device_model_names_apple_watches_from_the_one_table(code: str, name: str) -> None:
    assert humanize_device_model(code) == name


@pytest.mark.parametrize(
    ("device_model", "writers", "expected"),
    [
        # A watch is named by its model.
        ("Watch8,1", ("MD Apple Watch Ultra 4",), "Apple Watch Ultra 4 49mm"),
        # An app relaying another maker's device through the phone: name the maker.
        ("iPhone18,1", ("Oura",), "Oura"),
        ("iPhone18,1", ("Connect", "Garmin"), "Garmin"),
        ("iPhone18,1", (None, "Whoop"), "Whoop"),
        # The phone's own data: "iPhone", never "iPhone 17 Pro".
        ("iPhone18,1", ("Michael's iPhone",), "iPhone"),
        ("iPhone11,6", ("Clock",), "iPhone"),
        # The XML import stores HKDevice's bare model rather than the code.
        ("iPhone", ("Michael's iPhone",), "iPhone"),
        # A provider's own human-readable string is kept.
        ("Garmin fenix 8", (), "Garmin fenix 8"),
        (None, ("Oura",), None),
    ],
)
def test_source_device_name_never_names_a_phone_model(
    device_model: str | None, writers: tuple[str | None, ...], expected: str | None
) -> None:
    assert source_device_name(device_model, "apple", writers) == expected


def test_registry_device_name_calls_a_phone_iphone_unless_a_person_labelled_it() -> None:
    phone = {"model_display": "iPhone 17 Pro", "model_raw": "iPhone18,1", "device_type": "phone"}
    assert registry_device_name(label=None, **phone) == "iPhone"
    assert registry_device_name(label="Lab phone", label_source="manual", **phone) == "Lab phone"


def test_registry_device_name_names_an_unnamed_model_from_the_table_at_read_time() -> None:
    # Detection no longer copies the table's name onto the device, so a code the table
    # learns later reaches devices that already exist.
    assert registry_device_name(label=None, model_display=None, model_raw="Watch8,1") == "Apple Watch Ultra 4 49mm"
    # A name a person set still wins over the table's.
    assert (
        registry_device_name(label=None, model_display="Left wrist Ultra", model_raw="Watch8,1") == "Left wrist Ultra"
    )


def test_health_connect_is_not_resolved_as_garmin() -> None:
    """The bare "connect" keyword exists to catch Garmin Connect.

    It also matches "Health Connect", so an Android relay with no device_model used to
    resolve to brand Garmin - filing every Health Connect source under a maker the
    user may not own.
    """
    from app.schemas.enums import ProviderName
    from app.utils.device_registry import resolve_brand

    assert resolve_brand(ProviderName.HEALTH_CONNECT, None, "Health Connect") == "Health Connect"
    assert resolve_brand(ProviderName.HEALTH_CONNECT, None, "healthconnect") == "Health Connect"
    # Garmin Connect still resolves to Garmin.
    assert resolve_brand(ProviderName.APPLE, None, "Garmin Connect") == "Garmin"
    assert resolve_brand(ProviderName.APPLE, None, "Connect") == "Garmin"
