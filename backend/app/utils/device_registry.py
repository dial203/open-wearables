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
  Samsung ``SM-*``) to marketing names *for display only*. A code nothing names
  returns ``None`` so callers fall back to the raw identifier - we prefer showing the
  raw code over guessing wrong - except an Apple code newer than the table, which
  still says its family ("Apple Watch (Watch9,1)").

Phones are never named by model. A phone is not a device anyone is validating, and
on a relayed stream its model is the carrier of somebody else's data, so naming it
there sends the reader to the wrong hardware. See ``source_device_name()``.
"""

import re

from app.constants.devices_map import DEVICE_NAMES, HANDSET_DEVICE_TYPES, infer_device_type_from_model
from app.schemas.enums import DeviceType, IngestionRoute, ProviderName
from app.utils.device_naming import device_display_name

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
    "com.interaxon.muse": "Muse",
    "com.eightsleep": "Eight Sleep",
}

# --- App / HealthKit source-name keyword -> brand --------------------------------
# Substring match (case-insensitive) against the `source` string, e.g. Apple
# HealthKit source names like "Oura", "WHOOP", "Connect" (Garmin Connect).
SOURCE_NAME_BRANDS: tuple[tuple[str, str], ...] = (
    # "health connect" must precede the bare "connect" below, which is there to catch
    # Garmin Connect. Without it, a Health Connect source name with no device_model
    # resolves to brand "Garmin" - the substring matches - and every Android relay
    # gets filed under a maker the user may not own.
    ("health connect", "Health Connect"),
    ("healthconnect", "Health Connect"),
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
    ("muse", "Muse"),  # Interaxon Muse S: EEG headband relaying sleep into Apple Health
    ("dreem", "Dreem"),
    ("eight sleep", "Eight Sleep"),
    ("eightsleep", "Eight Sleep"),
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
    ("muse", "Muse"),
    ("dreem", "Dreem"),
)

# --- provider fallback (used when no source/model signal identifies a brand) -----
PROVIDER_BRANDS: dict[ProviderName, str] = {
    ProviderName.APPLE: "Apple",
    ProviderName.SAMSUNG: "Samsung",
    ProviderName.GARMIN: "Garmin",
    ProviderName.GOOGLE_HEALTH: "Google",
    ProviderName.HEALTH_CONNECT: "Health Connect",
    ProviderName.POLAR: "Polar",
    ProviderName.SUUNTO: "Suunto",
    ProviderName.WHOOP: "Whoop",
    ProviderName.STRAVA: "Strava",
    ProviderName.OURA: "Oura",
    ProviderName.FITBIT: "Fitbit",
    ProviderName.ULTRAHUMAN: "Ultrahuman",
    ProviderName.WITHINGS: "Withings",
}

# --- Aggregator platforms --------------------------------------------------------
# Providers that re-expose data recorded by *other* makers' devices, rather than
# serving only their own hardware. Data arriving through one of these has taken an
# extra hop, so it can differ from the maker's own API in freshness, completeness
# and rounding even though the brand is identical.
AGGREGATOR_PROVIDERS: frozenset[ProviderName] = frozenset(
    {
        ProviderName.APPLE,  # Apple Health / HealthKit
        ProviderName.GOOGLE_HEALTH,  # Google Health API (cloud)
        ProviderName.HEALTH_CONNECT,  # Android Health Connect (on-device SDK)
        ProviderName.SAMSUNG,  # Samsung Health
        ProviderName.STRAVA,  # activity platform fed by other devices
    }
)

# --- Apple productType families ---------------------------------------------------
# Apple codes are named from the full table (app/constants/devices_map/apple.py) and
# nowhere else. A second, shorter table here once named "Watch7,5" - the Ultra 2 - as a
# Series 8 and was consulted first, so every Ultra 2 in the system carried the wrong
# name. A code newer than the table still says what family it belongs to.
_APPLE_HARDWARE_CODE = re.compile(r"^(Watch|iPhone|iPad|iPod)\d+,\d+$")
_APPLE_FAMILIES: dict[str, str] = {"Watch": "Apple Watch", "iPhone": "iPhone", "iPad": "iPad", "iPod": "iPod"}

# --- Samsung / LG model code -> marketing name (display only) ---------------------
SAMSUNG_MODEL_NAMES: dict[str, str] = {
    "SM-S901U": "Galaxy S22",
    "SM-G973U1": "Galaxy S10",
    "SM-G975U": "Galaxy S10+",
    "SM-R830": "Galaxy Watch Active2",
    "SM-Q501": "Galaxy Ring",
    "LM-V350": "LG V35 ThinQ",
}


def resolve_brand_signal(
    provider: ProviderName,
    device_model: str | None = None,
    source: str | None = None,
) -> str | None:
    """The brand a signal actually names, or None when nothing did.

    Order: Android package (authoritative) -> device_model keyword -> source-name
    keyword. device_model is checked before the source label because the source is
    often a generic app/provider literal (e.g. "strava") while the model names the
    real recording device (e.g. "Garmin fenix 8").

    Separate from ``resolve_brand`` because the platform fallback and a real match
    are not the same answer, and one caller has to tell them apart: a third-party
    writer this table has never heard of ("Muse", "Hume", "Bevel") produced no match,
    and answering "Apple" there overwrites the only thing that named the recorder.
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

    return None


def resolve_brand(
    provider: ProviderName,
    device_model: str | None = None,
    source: str | None = None,
) -> str | None:
    """Derive a canonical brand, falling back to the platform when nothing names one.

    Returns None only for UNKNOWN/INTERNAL providers with no identifying signal.
    """
    return resolve_brand_signal(provider, device_model, source) or PROVIDER_BRANDS.get(provider)


def looks_like_writer_id(value: str | None) -> bool:
    """Whether a string is a package/bundle identifier rather than something readable.

    ``"com.ouraring.oura"`` is an identifier; ``"Muse"``, ``"Polar Flow"`` and
    ``"JOSHUA A's Apple Watch"`` are names a person would recognise. The test is a dot
    with no whitespace, which is what every package name and bundle id has and what no
    HealthKit source name observed so far does.

    Used to decide whether a caller's own source label is worth keeping when no brand
    table matched it: a name is, an identifier is not.
    """
    if not value:
        return False
    stripped = value.strip()
    return "." in stripped and not any(c.isspace() for c in stripped)


def relayed_brand(provider: ProviderName, writer_id: str | None) -> str | None:
    """Brand of a relayed stream, from the writing app alone.

    ``resolve_brand`` falls back to the platform's own brand when nothing in the writer
    id is recognised, which for a relayed stream would file a Muse headband under
    "Apple". The model is no help here - it names the phone that ran the app - so an
    unrecognised writer leaves the brand unset, which is the honest answer and the one a
    person can correct.
    """
    brand = resolve_brand(provider, None, writer_id)
    if brand is not None and brand == PROVIDER_BRANDS.get(provider):
        return None
    return brand


def resolve_ingestion_route(
    provider: ProviderName | str,
    original_source_name: str | None = None,
    device_model: str | None = None,
) -> IngestionRoute:
    """Whether a source reached us from its maker or via an aggregator platform.

    ``DIRECT`` when the provider is the maker's own API (Oura -> Oura), and also
    when an aggregator is carrying its *own* brand's data (an Apple Watch inside
    Apple Health is still first-party). ``AGGREGATOR`` when a platform is carrying
    another maker's data - the Oura/Garmin/Whoop rows that arrive via Apple or
    Google Health.

    An Apple Watch writes under the name its owner gave it ("JOSHUA A's Apple
    Watch", "MD Apple Watch Ultra 4"), which is not the platform's brand, so a name
    alone made every renamed watch read as relayed - and a consumer that keeps only
    first-party Apple HRV dropped those nights. Apple Watch hardware writing under a
    watch's own name is first-party. A writer that is an app, or a watch renamed
    past recognition, keeps the conservative answer below.
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

    if (
        provider_enum == ProviderName.APPLE
        and apple_hardware_family(device_model) == "Apple Watch"
        and "apple watch" in " ".join(original_source_name.split()).casefold()
    ):
        return IngestionRoute.DIRECT

    return IngestionRoute.AGGREGATOR


def apple_hardware_family(device_model: str | None) -> str | None:
    """The family an Apple hardware code belongs to ("Watch9,1" -> "Apple Watch")."""
    match = _APPLE_HARDWARE_CODE.match(device_model or "")
    return _APPLE_FAMILIES[match.group(1)] if match else None


def is_apple_hardware_code(device_model: str | None) -> bool:
    return apple_hardware_family(device_model) is not None


def humanize_device_model(device_model: str | None) -> str | None:
    """Map an opaque hardware code to a marketing name, for display only.

    Returns None for a code nothing names, so callers keep the raw identifier. An
    Apple code the table does not know yet is the exception: its family is certain
    from the code alone, so it reads "Apple Watch (Watch9,1)" until the code is added
    and the full name takes over everywhere at once.
    """
    if not device_model:
        return None
    if device_model in SAMSUNG_MODEL_NAMES:
        return SAMSUNG_MODEL_NAMES[device_model]
    if name := DEVICE_NAMES.get(device_model):
        return name
    if family := apple_hardware_family(device_model):
        return f"{family} ({device_model})"
    return None


def hardware_model_name(device_model: str | None) -> str | None:
    """The one model a hardware code identifies, or None where the code alone does not.

    An exact table hit only: a family fallback, a phone and a provider's free-text
    string identify nothing a consumer should file data under without asking.
    """
    if not device_model or is_handset_model(device_model):
        return None
    return SAMSUNG_MODEL_NAMES.get(device_model) or DEVICE_NAMES.get(device_model)


def is_handset_model(device_model: str | None) -> bool:
    """Whether a model string names a phone or tablet rather than something worn."""
    return bool(device_model) and infer_device_type_from_model(device_model) in HANDSET_DEVICE_TYPES


def handset_name(device_model: str | None) -> str:
    """A phone or tablet's name without its model: "iPhone", "iPad", else "Phone".

    Matched on the prefix rather than the full code, because the Apple XML import
    stores HKDevice's bare ``model`` ("iPhone") rather than the hardware code.
    """
    for family in ("iPhone", "iPad", "iPod"):
        if (device_model or "").startswith(family):
            return family
    return "Tablet" if infer_device_type_from_model(device_model) == DeviceType.TABLET else "Phone"


def source_device_name(
    device_model: str | None,
    provider: str | ProviderName | None = None,
    writers: tuple[str | None, ...] = (),
) -> str | None:
    """What to call the device behind a sample, never by a phone's model.

    Where the model string names a phone, the sample either came from an app that
    relayed another maker's device through it - and that maker, read off the writing
    app, is the device worth naming ("Oura", "Garmin") - or from the phone itself,
    which is called "iPhone" and no more. Everything else is the marketing name, or
    the provider's own string when nothing names it.
    """
    if not device_model:
        return None
    if not is_handset_model(device_model):
        return humanize_device_model(device_model) or device_model
    try:
        provider_enum = ProviderName(provider) if provider else None
    except ValueError:
        provider_enum = None
    if provider_enum is not None:
        for writer in writers:
            if brand := relayed_brand(provider_enum, writer):
                return brand
    return handset_name(device_model)


def registry_device_name(
    *,
    label: str | None,
    model_display: str | None,
    model_raw: str | None,
    brand_display: str | None = None,
    brand: str | None = None,
    device_type: str | None = None,
    label_source: str | None = None,
) -> str:
    """What to call a registry device on screen.

    ``device_display_name`` with two things this module knows and that one may not
    import: a model nobody named by hand is named from the hardware table at read time,
    so a code added to the table (or a name corrected there) reaches devices already
    stored; and a phone is called "iPhone" rather than by model unless a person
    labelled it.
    """
    if is_handset_model(model_raw):
        if label and label_source != "auto":
            return label
        return handset_name(model_raw)
    return device_display_name(
        label=label,
        model_display=model_display or humanize_device_model(model_raw),
        model_raw=model_raw,
        brand_display=brand_display,
        brand=brand,
        device_type=device_type,
        label_source=label_source,
    )


# --- Brand -> the provider that would deliver it directly -------------------------
# The inverse of PROVIDER_BRANDS, minus the aggregator platforms. A platform is left
# out deliberately: "Apple" reaching us through Apple Health is first-party data, not
# a relay of something we could read elsewhere, so it must never resolve to a direct
# alternative. What remains are the makers with an API of their own, which is exactly
# the set that makes an aggregator's copy redundant.
DIRECT_PROVIDER_BY_BRAND: dict[str, ProviderName] = {
    brand.casefold(): provider for provider, brand in PROVIDER_BRANDS.items() if provider not in AGGREGATOR_PROVIDERS
}


def direct_provider_for_brand(brand: str | None) -> ProviderName | None:
    """The maker's own provider for a brand string, or None when there is no direct route.

    ``None`` for a brand nothing here can serve directly - "Zepp", "Eight Sleep", a
    third-party HealthKit writer - which is the answer that keeps those relays visible:
    the aggregator is the only way that data reaches us at all.
    """
    if not brand:
        return None
    return DIRECT_PROVIDER_BY_BRAND.get(brand.strip().casefold())
