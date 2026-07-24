"""Device type enum and priority configuration."""

from enum import StrEnum


class DeviceType(StrEnum):
    """Type of device that collected health data."""

    CHEST_STRAP = "chest_strap"
    WATCH = "watch"
    BAND = "band"
    PHONE = "phone"
    SCALE = "scale"
    RING = "ring"
    OTHER = "other"
    UNKNOWN = "unknown"


# System-wide default device type priority (lower = higher priority)
# Used when user hasn't set custom priorities.
# Chest straps rank first: an ECG chest strap is the reference standard for heart
# rate / beat-to-beat data, ahead of optical wrist and finger sensors.
DEFAULT_DEVICE_TYPE_PRIORITY: dict[DeviceType, int] = {
    DeviceType.CHEST_STRAP: 1,
    DeviceType.WATCH: 2,
    DeviceType.BAND: 3,
    DeviceType.RING: 4,
    DeviceType.PHONE: 5,
    DeviceType.SCALE: 6,
    DeviceType.OTHER: 7,
    DeviceType.UNKNOWN: 99,
}

# Chest-strap model tokens. Matched as whole tokens so a bare "h10" can't be hit
# by an unrelated substring. Polar H-series and Garmin HRM are the common ECG straps.
_CHEST_STRAP_TOKENS: frozenset[str] = frozenset({"h9", "h10", "h7", "hrm", "hrm-pro", "hrm-dual", "hrm-fit", "tickr"})

# Optical ARM bands that are not chest straps — matched before the generic
# keyword pass so they land on BAND rather than falling through.
_ARM_BAND_KEYWORDS: tuple[str, ...] = ("verity sense", "oh1", "rhythm+", "rhythm 24")


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

    # Common keywords
    if "watch" in model_lower:
        return DeviceType.WATCH
    if "band" in model_lower or "vivosmart" in model_lower or "vivofit" in model_lower:
        return DeviceType.BAND
    if "ring" in model_lower or "oura" in model_lower:
        return DeviceType.RING
    if "phone" in model_lower:
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

    return DeviceType.UNKNOWN
