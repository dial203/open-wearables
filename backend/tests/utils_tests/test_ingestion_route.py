"""Direct-from-maker vs relayed-through-an-aggregator classification."""

import pytest

from app.schemas.enums import IngestionRoute, ProviderName
from app.utils.device_registry import resolve_ingestion_route


@pytest.mark.parametrize(
    ("provider", "brand", "expected"),
    [
        # Maker's own API — always direct.
        (ProviderName.OURA, "Oura", IngestionRoute.DIRECT),
        (ProviderName.GARMIN, "Garmin", IngestionRoute.DIRECT),
        (ProviderName.WHOOP, "Whoop", IngestionRoute.DIRECT),
        (ProviderName.POLAR, "Polar", IngestionRoute.DIRECT),
        (ProviderName.FITBIT, "Fitbit", IngestionRoute.DIRECT),
        # Another maker's brand arriving through a platform — relayed.
        (ProviderName.APPLE, "Oura", IngestionRoute.AGGREGATOR),
        (ProviderName.APPLE, "Whoop", IngestionRoute.AGGREGATOR),
        (ProviderName.APPLE, "Garmin", IngestionRoute.AGGREGATOR),
        (ProviderName.HEALTH_CONNECT, "Oura", IngestionRoute.AGGREGATOR),
        (ProviderName.GOOGLE_HEALTH, "Fitbit", IngestionRoute.AGGREGATOR),
        (ProviderName.SAMSUNG, "Polar", IngestionRoute.AGGREGATOR),
        (ProviderName.STRAVA, "Garmin", IngestionRoute.AGGREGATOR),
        # A platform carrying its own brand is still first-party.
        (ProviderName.APPLE, "Apple", IngestionRoute.DIRECT),
        (ProviderName.GOOGLE_HEALTH, "Google", IngestionRoute.DIRECT),
        (ProviderName.HEALTH_CONNECT, "Health Connect", IngestionRoute.DIRECT),
        (ProviderName.SAMSUNG, "Samsung", IngestionRoute.DIRECT),
        (ProviderName.STRAVA, "Strava", IngestionRoute.DIRECT),
        # Conservative: never claim "relayed" without a brand to name.
        (ProviderName.APPLE, None, IngestionRoute.DIRECT),
        (ProviderName.GOOGLE_HEALTH, "", IngestionRoute.DIRECT),
    ],
)
def test_resolve_ingestion_route(provider: ProviderName, brand: str | None, expected: IngestionRoute) -> None:
    assert resolve_ingestion_route(provider, brand) == expected


def test_brand_match_is_case_and_whitespace_insensitive() -> None:
    assert resolve_ingestion_route(ProviderName.APPLE, "  apple ") == IngestionRoute.DIRECT
    assert resolve_ingestion_route(ProviderName.APPLE, "APPLE") == IngestionRoute.DIRECT


def test_accepts_plain_provider_strings() -> None:
    """DataSource rows load `provider` as a plain str, not the enum."""
    assert resolve_ingestion_route("apple", "Oura") == IngestionRoute.AGGREGATOR
    assert resolve_ingestion_route("oura", "Oura") == IngestionRoute.DIRECT
    # An unrecognised provider must not raise.
    assert resolve_ingestion_route("not_a_provider", "Oura") == IngestionRoute.DIRECT


@pytest.mark.parametrize(
    ("writer", "device_model", "expected"),
    [
        # A watch writes under the name its owner gave it, which is not "Apple".
        ("JOSHUA A's Apple Watch", "Watch8,1", IngestionRoute.DIRECT),
        ("MD Apple Watch Ultra 4", "Watch8,1", IngestionRoute.DIRECT),
        ("Michael’s Apple Watch", "Watch7,5", IngestionRoute.DIRECT),
        # An app on the watch, a watch renamed past recognition, and the phone stay
        # where the name alone puts them.
        ("AutoSleep", "Watch7,5", IngestionRoute.AGGREGATOR),
        ("Josh's Ultra", "Watch8,1", IngestionRoute.AGGREGATOR),
        ("Oura", "iPhone18,1", IngestionRoute.AGGREGATOR),
        ("Michael's Apple Watch", None, IngestionRoute.AGGREGATOR),
    ],
)
def test_an_apple_watch_under_its_own_name_is_first_party(
    writer: str, device_model: str | None, expected: IngestionRoute
) -> None:
    assert resolve_ingestion_route(ProviderName.APPLE, writer, device_model) == expected
