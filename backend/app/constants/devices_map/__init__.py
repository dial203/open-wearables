from .apple import APPLE_DEVICE_NAMES
from .device_types import (
    HANDSET_DEVICE_TYPES,
    device_type_from_platform_report,
    infer_device_type,
    infer_device_type_from_model,
    infer_device_type_from_source_name,
    map_reported_device_type,
    reconcile_device_type,
)
from .samsung import SAMSUNG_DEVICE_NAMES

DEVICE_NAMES: dict[str, str] = {**APPLE_DEVICE_NAMES, **SAMSUNG_DEVICE_NAMES}


def resolve_device_name(device_model: str | None) -> str | None:
    """Marketing name for a raw device model, falling back to the raw value.

    Providers that report a model string we do not map (Garmin, Polar, Suunto)
    already send a human-readable name, so echoing the input is the right
    fallback - only a missing model yields None.
    """
    if not device_model:
        return None
    return DEVICE_NAMES.get(device_model, device_model)


__all__ = [
    "HANDSET_DEVICE_TYPES",
    "APPLE_DEVICE_NAMES",
    "DEVICE_NAMES",
    "SAMSUNG_DEVICE_NAMES",
    "device_type_from_platform_report",
    "infer_device_type",
    "infer_device_type_from_model",
    "infer_device_type_from_source_name",
    "map_reported_device_type",
    "reconcile_device_type",
    "resolve_device_name",
]
