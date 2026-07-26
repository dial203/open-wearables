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
        (ProviderName.GOOGLE, "Oura", IngestionRoute.AGGREGATOR),
        (ProviderName.GOOGLE, "Fitbit", IngestionRoute.AGGREGATOR),
        (ProviderName.SAMSUNG, "Polar", IngestionRoute.AGGREGATOR),
        (ProviderName.STRAVA, "Garmin", IngestionRoute.AGGREGATOR),
        # A platform carrying its own brand is still first-party.
        (ProviderName.APPLE, "Apple", IngestionRoute.DIRECT),
        (ProviderName.GOOGLE, "Google", IngestionRoute.DIRECT),
        (ProviderName.SAMSUNG, "Samsung", IngestionRoute.DIRECT),
        (ProviderName.STRAVA, "Strava", IngestionRoute.DIRECT),
        # Conservative: never claim "relayed" without a brand to name.
        (ProviderName.APPLE, None, IngestionRoute.DIRECT),
        (ProviderName.GOOGLE, "", IngestionRoute.DIRECT),
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
