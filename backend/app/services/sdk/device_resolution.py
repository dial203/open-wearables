"""Device resolution utilities for mobile SDK data (HealthKit, Health Connect, Samsung Health)."""

from app.schemas.enums import DeviceType, device_type_from_platform_report
from app.schemas.providers.mobile_sdk import OSVersion, SourceInfo


def _format_os_version(os_version: OSVersion | None) -> str | None:
    if not os_version:
        return None
    return f"{os_version.major_version}.{os_version.minor_version}.{os_version.patch_version}"


def _get_device_model(source: SourceInfo | None) -> str | None:
    if not source:
        return None
    if source.device_model:
        return source.device_model
    if source.product_type:
        return source.product_type
    return None


def _get_original_source_name(source: SourceInfo | None) -> str | None:
    if not source:
        return None
    if source.name:
        return source.name
    if source.device_name:
        return source.device_name
    # Third-party Health Connect writers (Peloton, Strava, Zwift, etc.)
    # never set ``name`` or ``device_name`` — they only populate the
    # writer's package identifier via ``appId`` (HC SDK ``DataOrigin``)
    # or ``bundleIdentifier`` (HealthKit Source bundle id). Falling back
    # to those keeps the workout's original provenance instead of
    # collapsing it to "unknown".
    if source.app_id:
        return source.app_id
    if source.bundle_identifier:
        return source.bundle_identifier
    return None


def extract_device_info(source: SourceInfo | None) -> tuple[str | None, str | None, str | None]:
    """Extract device information from SourceInfo.

    Returns:
        Tuple of (device_model, software_version, original_source_name).
    """
    if not source:
        return None, None, None

    device_model = _get_device_model(source)
    software_version = _format_os_version(source.operating_system_version)
    original_source_name = _get_original_source_name(source)  # e.g. "Apple Watch (Jan)" or "Zepp Life"

    return device_model, software_version, original_source_name


def extract_reported_device_type(source: SourceInfo | None) -> DeviceType | None:
    """The device type the platform declared for this source, if it declared one.

    Only Health Connect ever does. Its ``Metadata.device`` carries a type enum
    alongside the manufacturer and model, so a writer that populates it has told us
    what kind of hardware produced the samples rather than leaving us to read it out
    of a model string. HealthKit has no equivalent field on ``HKDevice``, so this is
    None for everything arriving by the Apple route.

    None means the platform said nothing, which is not the same as "unknown" - see
    device_type_from_platform_report.
    """
    if source is None:
        return None
    return device_type_from_platform_report(getattr(source, "device_type", None))
