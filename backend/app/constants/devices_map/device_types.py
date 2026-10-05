"""Device type inference from provider, device model and source name."""

import re
import unicodedata
from logging import getLogger

from app.schemas.enums import DeviceType, ProviderName
from app.utils.structured_logging import log_structured

from .samsung import SAMSUNG_DEVICE_NAMES

logger = getLogger(__name__)

# Providers that only ship a single form factor; model matching is skipped
SINGLE_DEVICE_PROVIDER_TYPE: dict[ProviderName, DeviceType] = {
    ProviderName.OURA: DeviceType.RING,
    ProviderName.ULTRAHUMAN: DeviceType.RING,
    ProviderName.WHOOP: DeviceType.BAND,
}

# Reported device types: mobile SDK deviceType (Health Connect Device.type, incl. extended
# types) and Google Health dataSource.device.formFactor, lowercased
REPORTED_DEVICE_TYPE_MAP: dict[str, DeviceType] = {
    "phone": DeviceType.PHONE,
    "watch": DeviceType.WATCH,
    "ring": DeviceType.RING,
    "scale": DeviceType.SCALE,
    "fitness_band": DeviceType.BAND,
    "chest_strap": DeviceType.CHEST_STRAP,
    # Fork: HEADBAND, not HEAD_MOUNTED. The fork filed Health Connect's head_mounted
    # as HEADBAND before upstream added HEAD_MOUNTED, and existing rows carry it; a
    # second name for the same report would split one device's history across two
    # ranks. A model string naming an EEG product still promotes it to EEG.
    "head_mounted": DeviceType.HEADBAND,
    "smart_display": DeviceType.SMART_DISPLAY,
    "tablet": DeviceType.TABLET,
    "hearable": DeviceType.HEADPHONES,
    "glasses": DeviceType.GLASSES,
    "fitness_machine": DeviceType.FITNESS_MACHINE,
    "fitness_equipment": DeviceType.FITNESS_MACHINE,
    "consumer_medical_device": DeviceType.OTHER,
    "meter": DeviceType.OTHER,
    "portable_computer": DeviceType.OTHER,
}

# Apple productType codes, case-sensitive
APPLE_PRODUCT_TYPE_PREFIXES: list[tuple[str, DeviceType]] = [
    ("Watch", DeviceType.WATCH),
    ("iPhone", DeviceType.PHONE),
    ("iPad", DeviceType.TABLET),
]

# Samsung model code prefix (SM-X...); SM-R is skipped as it mixes watches, bands and buds
SAMSUNG_MODEL_CODE = re.compile(r"^SM-([A-Z])\d")
SAMSUNG_MODEL_PREFIX_DEVICE_TYPE: dict[str, DeviceType] = {
    "L": DeviceType.WATCH,
    "Q": DeviceType.RING,
    **dict.fromkeys("SAMGFNE", DeviceType.PHONE),
    **dict.fromkeys("XTP", DeviceType.TABLET),
}

# Substrings of the normalized model (lowercased, accents stripped); first match wins, so
# order matters. Keywords starting with \b or ^ are regexes anchored to a word or string start.
#
# Fork additions are marked. Two upstream rules are deliberately changed:
# - Optical arm sensors (Verity Sense, OH1, Scosche Rhythm) stay BAND rather than
#   HR_SENSOR. The fork ranks BAND third; HR_SENSOR is an extended type ranked below
#   every wearable (see DEFAULT_DEVICE_TYPE_PRIORITY), so adopting it would demote an
#   arm-worn optical sensor below a ring when data priority resolves a conflict.
# - EEG products and headbands are matched before "band" and "headphone", so
#   "Muse S Headband" is not filed as a wrist band.
DEVICE_MODEL_KEYWORDS: list[tuple[tuple[str, ...], DeviceType]] = [
    # ECG chest straps. Fork: "chest", Wahoo TICKR and a bare H7/H9/H10 anywhere in
    # the string ("Polar H10 1A2B3C"), not only at its start.
    (
        (r"\bpolar h", r"\bh(7|9|10)\b", "hrm", "heart rate belt", "chest", "tickr"),
        DeviceType.CHEST_STRAP,
    ),
    (("verity sense", "oh1", "rhythm+", "rhythm 24"), DeviceType.BAND),  # fork: BAND, not HR_SENSOR
    # Fork: consumer EEG wearables, named by product family rather than inferred from
    # "headband" - the modality is what earns EEG its rank, and a headband can just as
    # easily be an optical forehead sensor.
    (("muse s", "muse 2", "muse-", "dreem", "frenz", "elemind", "somnee", r"\bmuse\b", r"\beeg\b"), DeviceType.EEG),
    (("headband", "head band"), DeviceType.HEADBAND),  # fork
    (("headphone", "earphone", "suunto wing", "airpods"), DeviceType.HEADPHONES),
    (("index bpm", "blood pressure"), DeviceType.BP_MONITOR),
    (("index sleep",), DeviceType.SLEEP_MONITOR),
    (("garmin edge", r"^edge \d"), DeviceType.BIKE_COMPUTER),
    (("watch", "moto 360"), DeviceType.WATCH),
    (("buds",), DeviceType.HEADPHONES),
    (
        (
            "band",
            "vivosmart",
            "vivofit",
            "charge",
            "inspire",
            "luxe",
            "alta",
            "fitbit air",
            "galaxy fit",
            "polar loop",
            "polar 360",
        ),
        DeviceType.BAND,
    ),
    ((r"\bring", "oura"), DeviceType.RING),
    (("ipad", "galaxy tab", "pixel tablet"), DeviceType.TABLET),
    (("phone", "pixel", "galaxy", "motorola", "moto ", r"^lm-"), DeviceType.PHONE),  # fork: LG "LM-" codes
    (("scale", "index s2"), DeviceType.SCALE),
    # Garmin
    (
        (
            "forerunner",
            "fenix",
            "venu",
            "epix",
            "enduro",
            "instinct",
            "tactix",
            "approach s",
            "vivoactive",
            "vivomove",
        ),
        DeviceType.WATCH,
    ),
    # Fork: COROS, which Strava reports verbatim ("COROS PACE 3"); without it a COROS
    # watch falls through to OTHER and ranks below every device whose modality is known.
    (("coros", "apex", "vertix", "pace 3", "pace pro"), DeviceType.WATCH),
    # Fitbit
    (("versa", "sense", "ionic"), DeviceType.WATCH),
    # Polar
    (("vantage", "grit x", "polar pacer", "pacer pro", "ignite", "unite"), DeviceType.WATCH),
    # Suunto model names always carry the brand ("Suunto Race 2")
    (("suunto",), DeviceType.WATCH),
    (("whoop",), DeviceType.BAND),
]

# Substrings of the normalized source name, for aggregated data without a model
SOURCE_NAME_KEYWORDS: list[tuple[tuple[str, ...], DeviceType]] = [
    (("autosleep",), DeviceType.WATCH),  # AutoSleep requires Apple Watch
    (("mi band", "xiaomi", "amazfit band"), DeviceType.BAND),
    (("oura",), DeviceType.RING),
]


def _keyword_pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(k if k.startswith((r"\b", "^")) else re.escape(k) for k in keywords))


_MODEL_PATTERNS = [(_keyword_pattern(k), t) for k, t in DEVICE_MODEL_KEYWORDS]
_SOURCE_PATTERNS = [(_keyword_pattern(k), t) for k, t in SOURCE_NAME_KEYWORDS]


def _normalize(value: str) -> str:
    """Lowercase and strip accents, so Garmin's "fēnix" / "vívoactive" match."""
    return "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))


# Longest code first, so a regional suffix ("SM-R860N") still finds its base code
_SAMSUNG_CODES = sorted(SAMSUNG_DEVICE_NAMES, key=len, reverse=True)


def _samsung_marketing_name(model_code: str) -> str | None:
    return next((SAMSUNG_DEVICE_NAMES[code] for code in _SAMSUNG_CODES if model_code.startswith(code)), None)


def _match_keywords(value: str, rules: list[tuple[re.Pattern[str], DeviceType]]) -> DeviceType | None:
    for pattern, device_type in rules:
        if pattern.search(value):
            return device_type
    return None


def infer_device_type_from_model(device_model: str | None) -> DeviceType:
    """Infer device type from a device model string (Apple productType codes, Samsung codes, keywords)."""
    if not device_model or device_model.strip().lower() == "unknown":
        return DeviceType.UNKNOWN

    for prefix, device_type in APPLE_PRODUCT_TYPE_PREFIXES:
        if device_model.startswith(prefix):
            return device_type

    if match := SAMSUNG_MODEL_CODE.match(device_model.upper()):
        if device_type := SAMSUNG_MODEL_PREFIX_DEVICE_TYPE.get(match.group(1)):
            return device_type
        # Unmapped prefixes (SM-R) mix watches, bands and buds; resolve listed codes by name
        if name := _samsung_marketing_name(device_model.upper()):
            return _match_keywords(_normalize(name), _MODEL_PATTERNS) or DeviceType.OTHER

    return _match_keywords(_normalize(device_model), _MODEL_PATTERNS) or DeviceType.OTHER


def infer_device_type_from_source_name(source_name: str | None) -> DeviceType:
    """Infer device type from a source/device name (e.g. "Galaxy Watch5", or Zepp Life via Apple Health)."""
    if not source_name:
        return DeviceType.UNKNOWN
    name = _normalize(source_name)
    return _match_keywords(name, _SOURCE_PATTERNS) or _match_keywords(name, _MODEL_PATTERNS) or DeviceType.UNKNOWN


def map_reported_device_type(reported: str | None) -> DeviceType | None:
    """Map an SDK deviceType or Google formFactor to DeviceType; unknown or unmapped values yield None."""
    if not reported:
        return None
    return REPORTED_DEVICE_TYPE_MAP.get(str(reported).lower())


def infer_device_type(
    provider: ProviderName,
    device_model: str | None,
    original_source_name: str | None = None,
    reported_type: DeviceType | None = None,
) -> DeviceType:
    """Resolve device type: single-device provider, then the reported type, then inference."""
    device_type = _resolve_device_type(provider, device_model, original_source_name, reported_type)
    if device_type == DeviceType.OTHER:
        log_structured(
            logger,
            "warning",
            f"Device mapped to other: {device_model}",
            provider=provider.value,
            action="device_type_other",
            device_model=device_model,
            original_source_name=original_source_name,
            reported_type=reported_type,
        )
    return device_type


def _resolve_device_type(
    provider: ProviderName,
    device_model: str | None,
    original_source_name: str | None,
    reported_type: DeviceType | None,
) -> DeviceType:
    if provider in SINGLE_DEVICE_PROVIDER_TYPE:
        return SINGLE_DEVICE_PROVIDER_TYPE[provider]

    inferred = infer_device_type_from_model(device_model)
    # Cloud providers store their own slug as the source name; it says nothing about the device
    if inferred in (DeviceType.UNKNOWN, DeviceType.OTHER) and (original_source_name or "").lower() != provider.value:
        from_name = infer_device_type_from_source_name(original_source_name)
        if from_name != DeviceType.UNKNOWN:
            inferred = from_name

    return reconcile_device_type(reported_type, inferred)


# Where a model string may refine the platform's classification instead of losing to
# it (fork). The platform report is a fact the writer asserted and inference is a guess
# about a string, so the guess wins only where it adds the modality the platform has no
# field for: Health Connect says where a device sits, never what it measures, so an
# optical forehead sensor and a Muse both arrive as head_mounted, and only one of them
# is a reference instrument for sleep staging. FITNESS_BAND against a model reading
# "Polar H10" looks like the same case and is not - there the writer had CHEST_STRAP
# available and chose otherwise.
_PLATFORM_TYPE_REFINEMENTS: dict[DeviceType, frozenset[DeviceType]] = {
    DeviceType.HEADBAND: frozenset({DeviceType.EEG}),
}

# Device types that run apps and relay what other hardware recorded. A model string
# naming one of these on a relay route describes the carrier, not the instrument (see
# services/devices/identity.relaying_host_model). TABLET joined PHONE when upstream
# split iPads out of PHONE (#1721); an iPad running the Muse app relays exactly as an
# iPhone does.
HANDSET_DEVICE_TYPES: frozenset[DeviceType] = frozenset({DeviceType.PHONE, DeviceType.TABLET})

# Reported types too vague to stand against a specific inference. Old Samsung SDKs
# report watches as phone and iOS reports iPads as phone; "other" names no category.
_VAGUE_REPORTED_TYPES: frozenset[DeviceType] = frozenset({DeviceType.PHONE, DeviceType.OTHER})


def device_type_from_platform_report(reported: object) -> DeviceType | None:
    """The device type a platform declared for itself, or None if it declared none.

    Accepts the raw string or an enum carrying one in ``.value`` (fork callers pass
    the SDK's ``SourceInfo.deviceType`` as is). None and ``UNKNOWN`` are different
    answers and are kept apart: a writer that passed no ``Device`` has said nothing,
    and overwriting a type inferred from a good model string with "unknown" would
    lose information to a field that was never filled in.
    """
    value = getattr(reported, "value", reported)
    if not isinstance(value, str):
        return None
    return map_reported_device_type(value.strip())


def reconcile_device_type(reported: DeviceType | None, inferred: DeviceType) -> DeviceType:
    """Combine a platform-declared device type with what the model strings infer.

    The platform's own report wins: it is the writer naming its hardware, where
    inference is pattern matching over a string that on a relayed stream describes
    the phone that carried the data. Two exceptions let the inference stand - a
    report too vague to mean much against a specific inference (upstream), and an
    inference strictly more specific about modality than the report can be (fork).
    """
    if reported is None or reported is DeviceType.UNKNOWN:
        return inferred
    if inferred in _PLATFORM_TYPE_REFINEMENTS.get(reported, frozenset()):
        return inferred
    if reported in _VAGUE_REPORTED_TYPES and inferred not in (DeviceType.UNKNOWN, DeviceType.OTHER):
        return inferred
    return reported
