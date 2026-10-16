from enum import Enum, StrEnum


class IngestionRoute(StrEnum):
    """How data reached Open Wearables — straight from the maker, or relayed.

    Consumers use this to tell "Oura read from Oura's own API" apart from "Oura
    data relayed through Apple Health", which are different in freshness,
    completeness and rounding even though both carry the same brand.
    """

    DIRECT = "direct"
    AGGREGATOR = "aggregator"


class ProviderName(str, Enum):
    """Supported data providers."""

    APPLE = "apple"
    SAMSUNG = "samsung"
    GARMIN = "garmin"
    HEALTH_CONNECT = "health_connect"
    GOOGLE_HEALTH = "google_health"
    POLAR = "polar"
    # Token holder for Polar's AccessLink v4 API, which is a separate OAuth server
    # (auth.polar.com) with scoped tokens — not a second Polar account. Declared
    # *after* POLAR on purpose: from_source_string matches by substring in
    # definition order, so a "polar_v4" source still resolves to POLAR and the data
    # it carries files under the user's existing Polar data source.
    POLAR_V4 = "polar_v4"
    SUUNTO = "suunto"
    WHOOP = "whoop"
    STRAVA = "strava"
    OURA = "oura"
    FITBIT = "fitbit"
    ULTRAHUMAN = "ultrahuman"
    SENSORBIO = "sensorbio"
    WITHINGS = "withings"
    UNKNOWN = "unknown"
    INTERNAL = "internal"

    @classmethod
    def from_source_string(cls, source: str | None) -> "ProviderName":
        """Infer provider from a source string by checking if provider name appears in it.

        Args:
            source: Source string (e.g., "apple_health_sdk", "Garmin Connect")

        Returns:
            Matching ProviderName or UNKNOWN if no match found
        """
        if not source:
            return cls.UNKNOWN

        source_lower = source.lower()
        # Check each provider (except UNKNOWN) to see if it appears in the source string
        for provider in cls:
            if provider in (cls.UNKNOWN, cls.INTERNAL):
                continue
            if provider.value in source_lower:
                return provider

        return cls.UNKNOWN


# Grants that ride on another provider's account rather than being a provider of their
# own: grant -> the provider whose account it belongs to. Polar's AccessLink v4 token is
# the beat-to-beat grant on the person's Polar account. These keep their own enum value
# (it names the OAuth server, the callback URL and the token row), but everywhere a
# provider is listed or shown they are folded into the host: one Polar, never two.
ADD_ON_GRANTS: dict[ProviderName, ProviderName] = {
    ProviderName.POLAR_V4: ProviderName.POLAR,
}


def is_add_on_grant(provider: str) -> bool:
    return provider in {grant.value for grant in ADD_ON_GRANTS}


def add_on_grants_of(host: str) -> list[str]:
    """The add-on grants that belong to ``host``'s accounts."""
    return [grant.value for grant, owner in ADD_ON_GRANTS.items() if owner.value == host]


# Default order for a fresh deployment. Covers every provider a user can connect;
# UNKNOWN and INTERNAL are deliberately absent because they are not configurable;
# add-on grants (POLAR_V4) are too, because they hold tokens and their data files under
# the host provider.
DEFAULT_PROVIDER_PRIORITY: dict[ProviderName, int] = {
    ProviderName.APPLE: 1,
    ProviderName.GARMIN: 2,
    ProviderName.POLAR: 3,
    ProviderName.SUUNTO: 4,
    ProviderName.WHOOP: 5,
    ProviderName.FITBIT: 6,
    ProviderName.GOOGLE_HEALTH: 7,
    ProviderName.HEALTH_CONNECT: 8,
    ProviderName.OURA: 9,
    ProviderName.SAMSUNG: 10,
    ProviderName.SENSORBIO: 11,
    ProviderName.STRAVA: 12,
    ProviderName.ULTRAHUMAN: 13,
    ProviderName.WITHINGS: 14,
}
