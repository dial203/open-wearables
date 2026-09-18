"""Extract per-route device identity claims from what providers actually send.

There is no identifier shared across ingest routes for the same physical unit, so
this module never tries to produce one. It produces *claims*: "on the oura route,
this device is ring configuration 4f2a..."; "on the apple route, it is whatever
writes as com.ouraring.oura". Claims are compared only against other claims from
the same route. Linking two routes is a separate, evidence-based decision that a
person confirms - see app/services/devices/detection.py.

``IdentityConfidence`` is the load-bearing distinction:

- STRONG means the provider issued the value, it is stable for the life of the
  unit, and two same-model devices owned by one user get different values. Only
  these may group data sources without a human.
- WEAK means the value names the writing app (``com.ouraring.oura``) or the
  hardware model (``fenix 8``, ``Watch7,5``). Two identical units share it, so it
  narrows the field and supports a proposal but never groups on its own.

Several identifiers providers *do* send were previously parsed and dropped. They
are collected here rather than at each call site so the rules stay in one place.

One rule is route-dependent rather than provider-dependent: on an aggregator route
the model string names the phone that relayed the data, not the device that recorded
it, so it is paired with the writing app's identifier before it may group anything.
See ``_grouping_claim``.
"""

from dataclasses import dataclass
from typing import Any

from app.schemas.enums import (
    WRITER_MODEL_SEPARATOR,
    DeviceIdentityKind,
    IdentityConfidence,
    ProviderName,
)

# Garmin's summaryId looks like "{devicePrefix}-{hex(startTimeInSeconds)}" with an
# optional third segment, and the leading token is stable across a user's uploads.
# Whether it is stable *per device* - the thing that would make it a real device
# identifier rather than an account one - has not been verified against production
# payloads, and getting it wrong would merge a user's watch and scale into one
# device with no visible symptom.
#
# So the prefix is recorded from the start (you cannot validate a signal you never
# stored) but enters as WEAK, which means it can support a link proposal and never
# groups anything on its own. Run scripts/validate_garmin_summary_prefix.py against
# real data; if prefixes separate cleanly by device, flip this to True and the same
# claims start grouping.
GARMIN_SUMMARY_PREFIX_VALIDATED = False


@dataclass(frozen=True)
class IdentityClaim:
    """One route's assertion about which device produced a batch of data."""

    route: str  # ProviderName value of the route that issued it
    kind: DeviceIdentityKind
    value: str
    confidence: IdentityConfidence

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("identity claim needs a value")


def _claim(
    route: ProviderName | str,
    kind: DeviceIdentityKind,
    value: str | None,
    confidence: IdentityConfidence,
) -> IdentityClaim | None:
    """Build a claim, or None when the provider sent nothing usable.

    Values are trimmed and length-capped to the column width. A provider that sends
    a 4 KB device name must not abort an import: the claim is the *key* we group on,
    so a truncated key still groups consistently, while a failed insert loses the
    whole batch.
    """
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return IdentityClaim(
        route=getattr(route, "value", route),
        kind=kind,
        value=cleaned[:255],
        confidence=confidence,
    )


def garmin_summary_prefix(summary_id: str | None) -> str | None:
    """Leading token of a Garmin summaryId, or None when it does not have the shape.

    ``"x153d4d8-5d5b43c0-900"`` -> ``"x153d4d8"``. Requires at least one dash and a
    non-empty prefix so a malformed or already-bare id cannot be mistaken for one.
    """
    if not summary_id or "-" not in summary_id:
        return None
    prefix = summary_id.split("-", 1)[0].strip()
    return prefix or None


def _grouping_claim(
    route: ProviderName | str,
    writer_id: str | None,
    model: str | None,
) -> IdentityClaim | None:
    """The one claim that may group data sources within this route.

    On a maker's own API the model string names the unit that recorded the data, and
    grouping on it reproduces what the provider already asserts. On an aggregator
    route it does not: HealthKit reports ``productType``, which is the handset that
    synced the batch, so a Muse headband, an Oura ring and a WHOOP band relayed
    through one iPhone all report ``iPhone15,3``. Grouping on that pools them into a
    single "device" - the exact over-merge this package exists to prevent, and one
    with no visible symptom until the samples are already mixed in an analysis.

    So where an aggregator names the writing app, the grouping key is the pair. Two
    phones relaying the same ring then read as two devices, which is an over-split: a
    person sees both and merges them, and no data was ever pooled in the meantime.
    """
    cleaned_writer = writer_id.strip() if writer_id else None
    cleaned_model = model.strip() if model else None

    if cleaned_writer and cleaned_model:
        return _claim(
            route,
            DeviceIdentityKind.AGGREGATOR_WRITER_MODEL,
            f"{cleaned_writer}{WRITER_MODEL_SEPARATOR}{cleaned_model}",
            IdentityConfidence.WEAK,
        )
    return _claim(route, DeviceIdentityKind.MODEL_STRING, cleaned_model, IdentityConfidence.WEAK)


def claims_from_sdk_source(provider: ProviderName | str, source: Any) -> list[IdentityClaim]:
    """Claims from a mobile SDK ``SourceInfo`` (HealthKit and Health Connect).

    HealthKit is the one route that sometimes carries a real device record: Apple's
    own hardware populates HKDevice with a name, model, hardware and software
    version, and ``deviceId``. Third-party apps writing into HealthKit generally
    pass no HKDevice at all, so for them only the bundle identifier survives - which
    names the app, not the unit, hence WEAK.

    ``deviceId`` is treated as strong but is the weakest of the strong signals:
    HKDevice.localIdentifier is a per-store UUID rather than a hardware serial, and
    Apple does not guarantee it survives a restore or a re-pair. The consequence of
    it changing is a split (a new device appears, and someone merges it), which is
    the recoverable direction.
    """
    if source is None:
        return []

    writer_id = getattr(source, "bundle_identifier", None) or getattr(source, "app_id", None)
    model = getattr(source, "device_model", None) or getattr(source, "device_name", None)

    claims = [
        _claim(provider, DeviceIdentityKind.HEALTHKIT_DEVICE_ID, _device_id(source), IdentityConfidence.STRONG),
        _claim(provider, DeviceIdentityKind.HEALTHKIT_BUNDLE, writer_id, IdentityConfidence.WEAK),
        _claim(
            provider,
            DeviceIdentityKind.APPLE_PRODUCT_TYPE,
            getattr(source, "product_type", None),
            IdentityConfidence.WEAK,
        ),
        # The model string groups on its own only when nothing names the writing app.
        # With a writer id present the pair is the grouping key instead, for the reason
        # in _grouping_claim: on these routes the model names the syncing handset.
        _grouping_claim(provider, writer_id, model),
    ]
    return [c for c in claims if c is not None]


def _device_id(source: Any) -> str | None:
    """HKDevice identifier, rejecting the pointer address the XML export yields.

    Apple Health XML describes HKDevice as ``<<HKDevice: 0x280b6b840>, name:Apple
    Watch, ...>``. The leading token is the object's address in the exporting
    process: different on every export, identical across unrelated devices in one
    export. Used as an identity it would both split one device across exports and
    merge different devices within an export, so anything that looks like an address
    is dropped rather than stored.
    """
    raw = getattr(source, "device_id", None)
    if raw is None:
        return None
    cleaned = str(raw).strip().rstrip(">")
    if not cleaned or cleaned.lower().startswith("0x"):
        return None
    return cleaned


def claims_from_oura_ring_config(config: dict[str, Any] | None) -> list[IdentityClaim]:
    """Claims from Oura's /v2/usercollection/ring_configuration.

    ``id`` is a genuine per-ring identifier and the strongest signal any route gives
    us for an Oura ring - it was already being parsed and thrown away, with only
    hardware_type and design used to build a display label.
    """
    if not config:
        return []
    claims = [
        _claim(ProviderName.OURA, DeviceIdentityKind.OURA_CONFIG_ID, config.get("id"), IdentityConfidence.STRONG),
        _claim(
            ProviderName.OURA,
            DeviceIdentityKind.MODEL_STRING,
            config.get("hardware_type"),
            IdentityConfidence.WEAK,
        ),
    ]
    return [c for c in claims if c is not None]


def claims_from_garmin_summary(summary_id: str | None) -> list[IdentityClaim]:
    """Claim from a Garmin Health API summaryId prefix.

    Garmin reports deviceName only on activities. Every wellness summary - sleep,
    dailies, epochs, HRV, pulse ox, body composition - carries no device at all,
    which is why a user's Garmin sleep and Garmin activities currently cannot be
    told apart by device. The summaryId prefix is the only per-record signal those
    endpoints carry.

    Enters WEAK until GARMIN_SUMMARY_PREFIX_VALIDATED says otherwise; see that
    constant for why.
    """
    prefix = garmin_summary_prefix(summary_id)
    confidence = IdentityConfidence.STRONG if GARMIN_SUMMARY_PREFIX_VALIDATED else IdentityConfidence.WEAK
    claim = _claim(ProviderName.GARMIN, DeviceIdentityKind.GARMIN_SUMMARY_PREFIX, prefix, confidence)
    return [claim] if claim else []


# Values that appear in `source` but name the integration rather than a writing app.
# Google stamps every row with one constant (GOOGLE_HEALTH_API_SOURCE) regardless of
# which app wrote it, so on that route the column carries no device information at all
# unless it happens to hold a real package name.
_PROVIDER_LITERALS: frozenset[str] = frozenset(
    {
        "google_health_api",
        "google_health",
        "health_connect",
        "apple_health_sdk",
        # What the Apple Health XML importer stamps on every row it writes
        # (XMLService._create_record). Missing here, it read as a writing app, so every
        # XML-imported source claimed the same constant identity: the first device to
        # take it owned it, and every later one collided and raised a link proposal
        # between unrelated hardware.
        "apple_health_xml",
        "healthkit",
        "api_response",
        "webhook",
    }
)

# Routes whose `source` can hold a writing app's identifier at all. These are also
# exactly the routes where a model string names the relaying handset rather than the
# recorder, so they are the routes _grouping_claim pairs writer with model on.
WRITER_ID_ROUTES: frozenset[str] = frozenset(
    {
        ProviderName.HEALTH_CONNECT.value,
        ProviderName.GOOGLE_HEALTH.value,
        ProviderName.APPLE.value,
        ProviderName.SAMSUNG.value,
    }
)

# The Android routes, whose writer id is a package name rather than a bundle id.
_ANDROID_ROUTES: frozenset[str] = frozenset({ProviderName.HEALTH_CONNECT.value, ProviderName.GOOGLE_HEALTH.value})


def _is_writer_id(provider_value: str, source: str) -> bool:
    """Whether `source` names an app that wrote the data, rather than the integration."""
    if provider_value not in WRITER_ID_ROUTES:
        return False
    normalized = source.strip().casefold()
    if normalized in _PROVIDER_LITERALS:
        return False
    # A provider's own key ("google", "apple") is the integration, not a writer.
    return normalized != provider_value


def claims_from_data_source(
    provider: ProviderName | str,
    device_model: str | None,
    source: str | None,
) -> list[IdentityClaim]:
    """Baseline claims derivable from what every data source already stores.

    This is the floor, not the ceiling: it runs for every ingest path so that a
    device always has at least the model-string claim the backfill seeded, and the
    route-specific extractors above add whatever else that route affords.
    """
    provider_value = getattr(provider, "value", provider)

    # A Health Connect / HealthKit writer id, which only these routes carry. Elsewhere
    # `source` is the provider's own literal ("garmin", "google_health_api") and says
    # nothing about a device, so claiming it would give every one of that user's
    # devices on that route the same identity value - noise at best, and a grouping
    # key that means "same provider" if it were ever promoted.
    writer_id = source if source and _is_writer_id(provider_value, source) else None

    claims = [_grouping_claim(provider, writer_id, device_model)]

    if writer_id:
        kind = (
            DeviceIdentityKind.HEALTH_CONNECT_PACKAGE
            if provider_value in _ANDROID_ROUTES
            else DeviceIdentityKind.HEALTHKIT_BUNDLE
        )
        claims.append(_claim(provider, kind, writer_id, IdentityConfidence.WEAK))

    return [c for c in claims if c is not None]
