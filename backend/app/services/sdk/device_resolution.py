"""Device resolution utilities for mobile SDK data (HealthKit, Health Connect, Samsung Health)."""

from app.schemas.enums import DeviceType, ProviderName, device_type_from_platform_report
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


# Routes whose ``deviceType`` is the platform's own classification of the hardware.
#
# Health Connect's ``Metadata.device`` carries a genuine type enum beside the
# manufacturer and model, and Samsung Health and the Google Health API expose the same
# field. On those routes a populated ``deviceType`` is the writer describing its own
# hardware, which is stronger evidence than anything inferred from a model string.
#
# Apple is deliberately absent. ``HKDevice`` has no category field at all, so nothing
# on that route can be a platform classification - see extract_reported_device_type.
_PLATFORM_TYPED_ROUTES: frozenset[str] = frozenset(
    {
        ProviderName.HEALTH_CONNECT.value,
        ProviderName.GOOGLE_HEALTH.value,
        ProviderName.SAMSUNG.value,
    }
)


def extract_reported_device_type(provider: ProviderName | str | None, source: SourceInfo | None) -> DeviceType | None:
    """The device type the platform declared for this source, if it declared one.

    Health Connect is the route this exists for: its ``Metadata.device`` carries a type
    enum alongside the manufacturer and model, so a writer that populates it has told us
    what kind of hardware produced the samples rather than leaving us to read it out of
    a model string.

    Apple returns None regardless of what arrives, and the route check is the whole
    point rather than an optimisation. ``HKDevice`` has no category field, so a
    ``deviceType`` on an Apple payload was not declared by HealthKit - the iOS SDK
    synthesises it, and at the versions in the field it synthesises it from
    ``HKSourceRevision.productType``, which names the handset. Every relayed stream
    therefore arrives claiming "phone".

    Taking that at face value undoes the fix that lets a relayed wearable outrank its
    carrier: a Muse headband resolves to EEG from the writing app's name, the payload
    says "phone", and the platform report - designed to beat inference, because on
    Health Connect it deserves to - overwrites the one reference instrument in a sleep
    study with the iPhone that synced it. A later SDK can report a real classification
    here; until a route's field is known to describe the recorder, silence is the honest
    answer.

    None means the platform said nothing, which is not the same as "unknown" - see
    device_type_from_platform_report.
    """
    if source is None:
        return None
    provider_value = getattr(provider, "value", provider)
    if provider_value not in _PLATFORM_TYPED_ROUTES:
        return None
    return device_type_from_platform_report(getattr(source, "device_type", None))
