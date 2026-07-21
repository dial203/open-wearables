"""Tests for canonical brand + human-model normalization (device_registry).

Covers the real-world source/model strings observed across ingest paths:
direct provider APIs, Apple HealthKit source names, and Google Health /
Health Connect Android package + device_model shapes.
"""

import pytest

from app.schemas.enums import ProviderName
from app.utils.device_registry import humanize_device_model, resolve_brand

P = ProviderName


@pytest.mark.parametrize(
    ("provider", "device_model", "source", "expected"),
    [
        # Google Health / Health Connect: Android package identifies the brand
        (P.GOOGLE, None, "com.ouraring.oura", "Oura"),
        (P.GOOGLE, None, "com.whoop.android", "Whoop"),
        (P.GOOGLE, None, "com.fitbit.FitbitMobile", "Fitbit"),
        (P.GOOGLE, None, "com.garmin.android.apps.connectmobile", "Garmin"),
        (P.GOOGLE, None, "com.sec.android.app.shealth", "Samsung Health"),
        (P.GOOGLE, "SM-S901U", "com.android.healthconnect.phone.jdef455", "Health Connect"),
        # Google 24/7 stream: source is the constant, brand comes from device_model
        (P.GOOGLE, "FITBIT", "google_health_api", "Fitbit"),
        (P.GOOGLE, "Versa 4", "google_health_api", "Fitbit"),
        (P.GOOGLE, "Google Pixel Watch 4 (45mm)", "google_health_api", "Google"),
        (P.GOOGLE, "HEALTH_CONNECT", "google_health_api", "Health Connect"),
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
def test_resolve_brand(provider, device_model, source, expected):
    assert resolve_brand(provider, device_model, source) == expected


def test_resolve_brand_unknown_provider_without_signal_returns_none():
    assert resolve_brand(P.UNKNOWN, None, None) is None


def test_humanize_device_model_maps_known_codes():
    assert humanize_device_model("iPhone10,5") == "iPhone 8 Plus"
    assert humanize_device_model("SM-S901U") == "Galaxy S22"


def test_humanize_device_model_unknown_returns_none():
    # Unknown codes fall back to the raw identifier (caller keeps device_model).
    assert humanize_device_model("Watch99,9") is None
    assert humanize_device_model(None) is None
