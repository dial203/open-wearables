"""Canonical brand + human-readable model normalization for data sources.

Non-destructive enrichment. The raw ``device_model`` captured from a provider is
never mutated - it is the experimental source of truth (users swap devices often,
so the exact hardware identifier per data source matters).

Two helpers:

- ``resolve_brand()`` derives a canonical brand ("Oura", "Whoop", "Fitbit", ...)
  from the strongest available signal, so the *same* physical brand is grouped
  regardless of the path it took (direct API vs Apple Health vs Google Health).
  It is used to populate ``DataSource.original_source_name`` when a provider did
  not supply one.

- ``humanize_device_model()`` maps opaque hardware codes (Apple ``productType``,
  Samsung ``SM-*``) to marketing names *for display only*. Unknown codes return
  ``None`` so callers fall back to the raw identifier - we prefer showing the raw
  code over guessing wrong.
"""

from app.constants.devices_map import DEVICE_NAMES
from app.schemas.enums import IngestionRoute, ProviderName

# --- Android package name -> brand (google/health-connect `source` values) -------
# Matched by exact value or prefix (Health Connect appends a per-record hash).
ANDROID_PACKAGE_BRANDS: dict[str, str] = {
    "com.ouraring.oura": "Oura",
    "com.whoop.android": "Whoop",
    "com.fitbit.FitbitMobile": "Fitbit",
    "com.garmin.android.apps.connectmobile": "Garmin",
    "com.sec.android.app.shealth": "Samsung Health",
    "com.google.android.apps.fitness": "Google Fit",
    "com.android.healthconnect": "Health Connect",
    "com.polar": "Polar",
    "com.suunto": "Suunto",
    "com.ultrahuman": "Ultrahuman",
}

# --- App / HealthKit source-name keyword -> brand --------------------------------
# Substring match (case-insensitive) against the `source` string, e.g. Apple
# HealthKit source names like "Oura", "WHOOP", "Connect" (Garmin Connect).
SOURCE_NAME_BRANDS: tuple[tuple[str, str], ...] = (
    ("oura", "Oura"),
    ("whoop", "Whoop"),
    ("zepp", "Zepp"),
    ("amazfit", "Zepp"),
    ("garmin connect", "Garmin"),
    ("connect", "Garmin"),
    ("garmin", "Garmin"),
    ("polar", "Polar"),
    ("suunto", "Suunto"),
    ("ultrahuman", "Ultrahuman"),
    ("peloton", "Peloton"),
    ("strava", "Strava"),
    ("fitbit", "Fitbit"),
    ("nike run club", "Nike"),
    ("myfitnesspal", "MyFitnessPal"),
    ("corsano", "Corsano"),
    ("masimo", "Masimo"),
    ("withings", "Withings"),
)

# --- device_model keyword -> brand (google `device_model`, generic models) -------
DEVICE_MODEL_BRANDS: tuple[tuple[str, str], ...] = (
    ("fitbit", "Fitbit"),
    ("versa", "Fitbit"),
    ("charge", "Fitbit"),
    ("sense", "Fitbit"),
    ("inspire", "Fitbit"),
    ("pixel watch", "Google"),
    ("galaxy", "Samsung"),
    ("fenix", "Garmin"),
    ("forerunner", "Garmin"),
    ("venu", "Garmin"),
    ("instinct", "Garmin"),
    ("epix", "Garmin"),
    ("garmin", "Garmin"),
    ("polar", "Polar"),
    ("suunto", "Suunto"),
    ("whoop", "Whoop"),
    ("health_connect", "Health Connect"),
)

# --- provider fallback (used when no source/model signal identifies a brand) -----
PROVIDER_BRANDS: dict[ProviderName, str] = {
    ProviderName.APPLE: "Apple",
    ProviderName.SAMSUNG: "Samsung",
    ProviderName.GARMIN: "Garmin",
    ProviderName.GOOGLE: "Google",
    ProviderName.POLAR: "Polar",
    ProviderName.SUUNTO: "Suunto",
    ProviderName.WHOOP: "Whoop",
    ProviderName.STRAVA: "Strava",
    ProviderName.OURA: "Oura",
    ProviderName.FITBIT: "Fitbit",
    ProviderName.ULTRAHUMAN: "Ultrahuman",
}

# --- Aggregator platforms --------------------------------------------------------
# Providers that re-expose data recorded by *other* makers' devices, rather than
# serving only their own hardware. Data arriving through one of these has taken an
# extra hop, so it can differ from the maker's own API in freshness, completeness
# and rounding even though the brand is identical.
AGGREGATOR_PROVIDERS: frozenset[ProviderName] = frozenset(
    {
        ProviderName.APPLE,  # Apple Health / HealthKit
        ProviderName.GOOGLE,  # Google Health / Health Connect
        ProviderName.SAMSUNG,  # Samsung Health
        ProviderName.STRAVA,  # activity platform fed by other devices
    }
)

# --- Apple productType -> marketing name (display only) --------------------------
APPLE_MODEL_NAMES: dict[str, str] = {
    "iPhone7,1": "iPhone 6 Plus",
    "iPhone7,2": "iPhone 6",
    "iPhone10,5": "iPhone 8 Plus",
    "iPhone11,8": "iPhone XR",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone15,3": "iPhone 14 Pro Max",
    "Watch3,4": "Apple Watch Series 3",
    "Watch4,2": "Apple Watch Series 4",
    "Watch6,2": "Apple Watch Series 6",
    "Watch7,5": "Apple Watch Series 8",
}

# --- Samsung / LG model code -> marketing name (display only) ---------------------
SAMSUNG_MODEL_NAMES: dict[str, str] = {
    "SM-S901U": "Galaxy S22",
    "SM-G973U1": "Galaxy S10",
    "SM-G975U": "Galaxy S10+",
    "SM-R830": "Galaxy Watch Active2",
    "SM-Q501": "Galaxy Ring",
    "LM-V350": "LG V35 ThinQ",
}


def resolve_brand(
    provider: ProviderName,
    device_model: str | None = None,
    source: str | None = None,
) -> str | None:
    """Derive a canonical brand from the strongest available signal.

    Order: Android package (authoritative) -> device_model keyword -> source-name
    keyword -> provider fallback. device_model is checked before the source label
    because the source is often a generic app/provider literal (e.g. "strava")
    while the model names the real recording device (e.g. "Garmin fenix 8").
    Returns None only for UNKNOWN/INTERNAL providers with no identifying signal.
    """
    if source:
        for pkg, brand in ANDROID_PACKAGE_BRANDS.items():
            if source == pkg or source.startswith(pkg):
                return brand

    if device_model:
        model_lower = device_model.lower()
        for keyword, brand in DEVICE_MODEL_BRANDS:
            if keyword in model_lower:
                return brand

    if source:
        source_lower = source.lower()
        for keyword, brand in SOURCE_NAME_BRANDS:
            if keyword in source_lower:
                return brand

    return PROVIDER_BRANDS.get(provider)


def resolve_ingestion_route(
    provider: ProviderName | str,
    original_source_name: str | None = None,
) -> IngestionRoute:
    """Whether a source reached us from its maker or via an aggregator platform.

    ``DIRECT`` when the provider is the maker's own API (Oura -> Oura), and also
    when an aggregator is carrying its *own* brand's data (an Apple Watch inside
    Apple Health is still first-party). ``AGGREGATOR`` when a platform is carrying
    another maker's data - the Oura/Garmin/Whoop rows that arrive via Apple or
    Google Health.

    Deliberately conservative: a source is only called an aggregator when a brand
    other than the platform's own can actually be named, so an unrecognised brand
    is never mislabelled as relayed.
    """
    try:
        provider_enum = ProviderName(provider)
    except ValueError:
        return IngestionRoute.DIRECT

    if provider_enum not in AGGREGATOR_PROVIDERS:
        return IngestionRoute.DIRECT

    platform_brand = PROVIDER_BRANDS.get(provider_enum)
    if not original_source_name or not platform_brand:
        return IngestionRoute.DIRECT

    if original_source_name.strip().casefold() == platform_brand.casefold():
        return IngestionRoute.DIRECT

    return IngestionRoute.AGGREGATOR


def humanize_device_model(device_model: str | None) -> str | None:
    """Map an opaque hardware code to a marketing name, for display only.

    Returns None for unknown codes so callers keep the raw identifier.
    """
    if not device_model:
        return None
    if device_model in APPLE_MODEL_NAMES:
        return APPLE_MODEL_NAMES[device_model]
    if device_model in SAMSUNG_MODEL_NAMES:
        return SAMSUNG_MODEL_NAMES[device_model]
    # The curated maps above only cover the handful of codes we name explicitly;
    # fall through to the full hardware registry so a current Apple Watch shows as
    # "Apple Watch Series 10 46mm (GPS)" rather than the raw "Watch7,9".
    return DEVICE_NAMES.get(device_model)
