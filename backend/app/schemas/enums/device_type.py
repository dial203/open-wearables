"""Device type enum and priority configuration."""

import re
from enum import StrEnum


class DeviceType(StrEnum):
    """Type of device that collected health data."""

    CHEST_STRAP = "chest_strap"
    EEG = "eeg"
    HEADBAND = "headband"
    WATCH = "watch"
    BAND = "band"
    PHONE = "phone"
    SCALE = "scale"
    RING = "ring"
    OTHER = "other"
    UNKNOWN = "unknown"


# System-wide default device type priority (lower = higher priority)
# Used when user hasn't set custom priorities.
# EEG ranks first and chest straps second: EEG is the reference standard for sleep
# staging and an ECG chest strap for beat-to-beat heart rate, both ahead of the
# optical wrist and finger sensors that infer the same quantities.
#
# The numbers below the two new types are deliberately left as they were rather than
# renumbered. DeviceTypePriorityRepository.initialize_defaults is additive: it inserts
# only the types missing from an already-seeded database and never rewrites a row an
# operator may have customised. Renumbering here would therefore apply to fresh
# databases only, and a new type inserted at a number an existing row already holds
# would tie with it, with nothing to break the tie. EEG takes 0 (above chest_strap)
# and HEADBAND takes 8 (below the types whose modality is known, above unknown) so
# both slot in correctly whether or not the table was seeded before they existed.
DEFAULT_DEVICE_TYPE_PRIORITY: dict[DeviceType, int] = {
    DeviceType.EEG: 0,
    DeviceType.CHEST_STRAP: 1,
    DeviceType.WATCH: 2,
    DeviceType.BAND: 3,
    DeviceType.RING: 4,
    DeviceType.PHONE: 5,
    DeviceType.SCALE: 6,
    DeviceType.OTHER: 7,
    # A headband whose sensing modality we cannot name says nothing about signal
    # quality on its own, so it ranks below every type that does and above unknown.
    DeviceType.HEADBAND: 8,
    DeviceType.UNKNOWN: 99,
}

# Chest-strap model tokens. Matched as whole tokens so a bare "h10" can't be hit
# by an unrelated substring. Polar H-series and Garmin HRM are the common ECG straps.
_CHEST_STRAP_TOKENS: frozenset[str] = frozenset({"h9", "h10", "h7", "hrm", "hrm-pro", "hrm-dual", "hrm-fit", "tickr"})

# Optical ARM bands that are not chest straps — matched before the generic
# keyword pass so they land on BAND rather than falling through.
_ARM_BAND_KEYWORDS: tuple[str, ...] = ("verity sense", "oh1", "rhythm+", "rhythm 24")

# Consumer EEG wearables, by product family. Named explicitly rather than inferred
# from the word "headband": the modality is what earns EEG its priority, and a
# headband can just as easily be an optical forehead sensor.
_EEG_KEYWORDS: tuple[str, ...] = ("muse s", "muse 2", "muse-", "dreem", "frenz", "elemind", "somnee")

# Whole tokens rather than substrings, so "eeg" cannot be hit inside an unrelated word.
_EEG_TOKENS: frozenset[str] = frozenset({"muse", "eeg"})

# Handset model codes, which name no body part and would otherwise fall through to
# OTHER. This matters beyond iconography: a relayed stream is recognised as relayed by
# its model naming a phone, and a Pixel or Galaxy handset that reads as OTHER leaves
# every Android app on it grouped as one device. Matched only after the wearable
# keywords below, so "Galaxy Watch" and "Galaxy Ring" are already claimed.
_PHONE_MODEL_RE = re.compile(r"^sm-[sgnaf]\d|^lm-|\bpixel(?! watch)\b|\bgalaxy (s|note|z|a)\d")


def infer_device_type_from_model(device_model: str | None) -> DeviceType:
    """Infer device type from device model string.

    Handles Apple productType codes and common device model patterns.
    """
    if not device_model:
        return DeviceType.UNKNOWN

    model_lower = device_model.lower()

    # Apple productType codes
    if device_model.startswith("Watch"):
        return DeviceType.WATCH
    if device_model.startswith("iPhone"):
        return DeviceType.PHONE
    if device_model.startswith("iPad"):
        return DeviceType.PHONE  # Treat iPad as phone for priority purposes

    # Chest straps (ECG) — checked before the generic keyword pass so models like
    # "Polar H10" or "HRM 600" aren't swallowed by the brand patterns below.
    if "chest" in model_lower:
        return DeviceType.CHEST_STRAP
    tokens = {t.strip("()[],") for t in model_lower.replace("_", " ").replace("/", " ").split()}
    if tokens & _CHEST_STRAP_TOKENS:
        return DeviceType.CHEST_STRAP
    if any(t.startswith("hrm") for t in tokens):
        return DeviceType.CHEST_STRAP

    # Optical arm bands (Polar Verity Sense / OH1, Scosche Rhythm) — not chest straps.
    if any(k in model_lower for k in _ARM_BAND_KEYWORDS):
        return DeviceType.BAND

    # EEG before the generic keyword pass below: "Muse S Headband" would otherwise be
    # swallowed by the "band" substring and filed as a wrist band.
    if any(k in model_lower for k in _EEG_KEYWORDS) or tokens & _EEG_TOKENS:
        return DeviceType.EEG
    if "headband" in model_lower or "head band" in model_lower:
        return DeviceType.HEADBAND

    # Common keywords
    if "watch" in model_lower:
        return DeviceType.WATCH
    if "band" in model_lower or "vivosmart" in model_lower or "vivofit" in model_lower:
        return DeviceType.BAND
    if "ring" in model_lower or "oura" in model_lower:
        return DeviceType.RING
    if "phone" in model_lower or _PHONE_MODEL_RE.search(model_lower):
        return DeviceType.PHONE
    if "scale" in model_lower or "index" in model_lower:
        return DeviceType.SCALE

    # Garmin device patterns
    if any(
        x in model_lower for x in ["forerunner", "fenix", "venu", "epix", "enduro", "instinct", "tactix", "approach"]
    ):
        return DeviceType.WATCH

    # Polar patterns
    if any(x in model_lower for x in ["vantage", "grit x", "pacer", "ignite", "unite"]):
        return DeviceType.WATCH

    # COROS patterns. Named because Strava reports these verbatim ("COROS PACE 3")
    # and without them a COROS watch falls through to OTHER, which ranks it below
    # every device whose modality is known when data priority resolves a conflict.
    if any(x in model_lower for x in ["coros", "apex", "vertix", "pace 3", "pace pro"]):
        return DeviceType.WATCH

    # Suunto patterns
    if any(x in model_lower for x in ["suunto", "vertical", "race", "peak"]):
        return DeviceType.WATCH

    # Whoop patterns
    if "whoop" in model_lower:
        return DeviceType.BAND

    return DeviceType.OTHER


def infer_device_type_from_source_name(source_name: str | None) -> DeviceType:
    """Infer device type from original source name (for aggregated data).

    Used when device_model is not available (e.g., data from Zepp Life via Apple Health).
    """
    if not source_name:
        return DeviceType.UNKNOWN

    name_lower = source_name.lower()

    # Known aggregator apps
    if "autosleep" in name_lower:
        return DeviceType.WATCH  # AutoSleep requires Apple Watch
    if "mi band" in name_lower or "xiaomi" in name_lower:
        return DeviceType.BAND
    if "amazfit band" in name_lower:
        return DeviceType.BAND
    if "oura" in name_lower:
        return DeviceType.RING
    if "zepp life" in name_lower:
        return DeviceType.UNKNOWN  # Could be watch or band
    if "health" in name_lower and "apple" not in name_lower:
        return DeviceType.UNKNOWN  # Manual entry

    # HealthKit / Health Connect source names are the *device* name the user gave the
    # hardware ("Ali's Apple Watch", "Ali's iPhone"), so when the provider sent no
    # hardware model the name is the only device signal there is. Reuse the model
    # keyword table rather than duplicating it; OTHER means "matched nothing there",
    # which for a source name is still UNKNOWN (an app name is not a device).
    inferred = infer_device_type_from_model(source_name)
    if inferred not in (DeviceType.OTHER, DeviceType.UNKNOWN):
        return inferred

    return DeviceType.UNKNOWN


# Health Connect's ``Device.type`` enum, as the mobile SDK relays it in
# ``SourceInfo.deviceType``, mapped onto the types this system ranks.
#
# Health Connect is the only route that reports a device *classification* rather
# than a model string: every record's ``Metadata`` may carry a ``Device`` with a
# manufacturer, a model and a type. HealthKit's ``HKDevice`` carries name,
# manufacturer, model and versions but no category field at all, so on the Apple
# route there is nothing here to consume and classification stays inference from
# strings - and a ``deviceType`` on an Apple payload is the iOS SDK's own guess, which
# services/sdk/device_resolution filters out by route before it reaches this map.
# Google's cloud Health API surfaces the same enum, which is why a data source can
# arrive whose entire model string is the literal "ring" or "watch".
_PLATFORM_DEVICE_TYPES: dict[str, DeviceType] = {
    "phone": DeviceType.PHONE,
    "watch": DeviceType.WATCH,
    "scale": DeviceType.SCALE,
    "ring": DeviceType.RING,
    "chest_strap": DeviceType.CHEST_STRAP,
    "fitness_band": DeviceType.BAND,
    # HEADBAND, not EEG. Health Connect says where a device sits, never what it
    # measures, and the EEG rank exists for the modality: an optical forehead
    # sensor and a Muse are both head-mounted, and only one of them is a reference
    # instrument for sleep staging. A model string that names an EEG product still
    # promotes it - see _PLATFORM_TYPE_REFINEMENTS.
    "head_mounted": DeviceType.HEADBAND,
    "smart_display": DeviceType.OTHER,
}


# Where a model string may refine the platform's classification instead of losing
# to it. Deliberately one entry: the platform report is a fact the writer asserted
# and inference is a guess about a string, so the guess wins only where it adds the
# modality the platform has no field for. FITNESS_BAND against a model reading
# "Polar H10" looks like the same case and is not - there the writer had
# CHEST_STRAP available and chose otherwise, and second-guessing that would
# re-open classification to exactly the string matching this map exists to bound.
_PLATFORM_TYPE_REFINEMENTS: dict[DeviceType, frozenset[DeviceType]] = {
    DeviceType.HEADBAND: frozenset({DeviceType.EEG}),
}


def device_type_from_platform_report(reported: object) -> DeviceType | None:
    """The device type a platform declared for itself, or None if it declared none.

    None and ``UNKNOWN`` are different answers and are kept apart: a writer that
    passed no ``Device`` has said nothing, and overwriting a type inferred from a
    good model string with "unknown" would lose information to a field that was
    never filled in. Only a recognised, non-unknown constant returns a type.
    """
    value = getattr(reported, "value", reported)
    if not isinstance(value, str):
        return None
    return _PLATFORM_DEVICE_TYPES.get(value.strip().casefold())


def reconcile_device_type(reported: DeviceType | None, inferred: DeviceType) -> DeviceType:
    """Combine a platform-declared device type with what the model strings infer.

    The platform's own report wins. It is the writer naming its hardware, where
    inference is pattern matching over a string that on a relayed stream describes
    the phone that carried the data. The exception is a refinement: where the
    inferred type is strictly more specific about modality than the reported one
    can be, it stands. See _PLATFORM_TYPE_REFINEMENTS.
    """
    if reported is None or reported is DeviceType.UNKNOWN:
        return inferred
    if inferred in _PLATFORM_TYPE_REFINEMENTS.get(reported, frozenset()):
        return inferred
    return reported
