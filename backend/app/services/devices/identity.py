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
"""

import re
from dataclasses import dataclass
from typing import Any

from app.schemas.enums import (
    DeviceIdentityKind,
    DeviceType,
    IdentityConfidence,
    ProviderName,
    infer_device_type_from_model,
    infer_device_type_from_source_name,
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
    ]

    # When the model describes the phone that relayed the data rather than the unit
    # that recorded it, neither it nor the productType behind it says anything about
    # this device, and both are shared by every app on that phone. Claiming them would
    # hand the first app to sync a claim the next app's sync then collides with.
    if relaying_host_model(provider, model, writer_id) is None:
        claims.append(
            _claim(
                provider,
                DeviceIdentityKind.APPLE_PRODUCT_TYPE,
                getattr(source, "product_type", None),
                IdentityConfidence.WEAK,
            )
        )
        claims.append(_claim(provider, DeviceIdentityKind.MODEL_STRING, model, IdentityConfidence.WEAK))

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
        "healthkit",
        "api_response",
        "webhook",
        # Health Connect stamps some rows with the OS itself rather than an app.
        "android",
    }
)

# Routes whose `source` can hold a writing app's identifier at all.
_WRITER_ID_ROUTES: frozenset[str] = frozenset(
    {
        ProviderName.HEALTH_CONNECT.value,
        ProviderName.GOOGLE_HEALTH.value,
        ProviderName.APPLE.value,
        ProviderName.SAMSUNG.value,
    }
)

# The Android routes, whose writer id is a package name rather than a bundle id.
_ANDROID_ROUTES: frozenset[str] = frozenset({ProviderName.HEALTH_CONNECT.value, ProviderName.GOOGLE_HEALTH.value})

# Writer ids belonging to the platform itself rather than to a third party. Data a
# platform's own app writes about the phone it runs on genuinely came from that phone,
# so the model string is the unit and must keep behaving as one.
_PLATFORM_OWN_WRITER_PREFIXES: dict[str, tuple[str, ...]] = {
    ProviderName.APPLE.value: ("com.apple.",),
    ProviderName.HEALTH_CONNECT.value: ("com.google.android.apps.fitness", "com.android.", "com.google.android.gms"),
    ProviderName.GOOGLE_HEALTH.value: ("com.google.android.apps.fitness", "com.android.", "com.google.android.gms"),
    ProviderName.SAMSUNG.value: ("com.sec.android.", "com.samsung."),
}


# Apple's own apps, by the display name HealthKit reports as the source. Their data is
# recorded by the iPhone or the Watch the person is wearing, not relayed from anywhere,
# so the model string is the unit. They are listed by name because HealthKit gives us a
# display name here far more often than the com.apple.* bundle id the prefixes above
# would catch.
_APPLE_FIRST_PARTY_WRITERS: frozenset[str] = frozenset(
    {
        "health",
        "fitness",
        "activity",
        "workout",
        "clock",
        "sleep",
        "breathe",
        "mindfulness",
        "cycle tracking",
        "siri",
        "home",
    }
)

# Tokens too generic to identify hardware. A writer sharing only one of these with the
# host's model says nothing: "Galaxy Watch" and "Galaxy S22" are two different devices.
_GENERIC_MODEL_TOKENS: frozenset[str] = frozenset(
    {
        "apple",
        "galaxy",
        "google",
        "lg",
        "samsung",
        "max",
        "mini",
        "plus",
        "pro",
        "se",
        "ultra",
        "my",
        "the",
    }
)


def _model_tokens(value: str) -> set[str]:
    """Identifying tokens of a device name, for comparing two names for the same unit."""
    raw = re.split(r"[^0-9a-z+]+", value.casefold())
    # Purely numeric tokens are dropped: "8" from "iPhone 8 Plus" would match any writer
    # name with an 8 in it. Model codes like "s22" and "v35" survive because they are not.
    return {t for t in raw if t and not t.isdigit() and t not in _GENERIC_MODEL_TOKENS}


def _writer_names_the_host(writer_id: str, device_model: str) -> bool:
    """Whether a writer id is another name for the hardware the model string describes.

    A platform often reports its own handset's data under the name the owner gave the
    phone, or under a model name it spells differently: "Michael's S22" against
    ``SM-S901U``, "V35 ThinQ" against ``LM-V350``, "S10+" against ``SM-G975U``. Those are
    the phone writing about itself, not something relayed through it, so splitting them
    off would turn one handset into several devices and throw its model away.

    Compared against the marketing name as well as the raw code, since the raw code is
    what the platform reports and the marketing name is what the owner named it after.
    """
    from app.utils.device_registry import humanize_device_model

    writer_tokens = _model_tokens(writer_id)
    if not writer_tokens:
        return False
    model_tokens = _model_tokens(device_model) | _model_tokens(humanize_device_model(device_model) or "")
    return bool(writer_tokens & model_tokens)


def _is_platform_own_writer(provider_value: str, writer_id: str, device_model: str) -> bool:
    """Whether a writer id belongs to the platform or the host rather than a third party."""
    normalized = writer_id.strip()
    if any(normalized.startswith(prefix) for prefix in _PLATFORM_OWN_WRITER_PREFIXES.get(provider_value, ())):
        return True
    if provider_value == ProviderName.APPLE.value and normalized.casefold() in _APPLE_FIRST_PARTY_WRITERS:
        return True
    # On HealthKit the writer id is usually the source's display name, which for the
    # platform's own data is the hardware's name ("Michael's iPhone"). A writer that
    # names a phone is the phone.
    if infer_device_type_from_source_name(normalized) is DeviceType.PHONE:
        return True
    return _writer_names_the_host(normalized, device_model)


def relaying_host_model(
    provider: ProviderName | str,
    device_model: str | None,
    writer_id: str | None,
) -> str | None:
    """The relaying phone's model, when a route's model string names the host not the unit.

    A third-party app writing into HealthKit or Health Connect generally passes no
    device record of its own, so the platform reports the phone that ran the app. The
    Muse app on an iPhone therefore sends ``iPhone 17 Pro`` as the model for data a
    headband recorded - and so does every other app on that phone.

    That is the one case where the provider's model string is not evidence about this
    device at all. Taken at face value it is worse than no signal: grouping on it
    collapses every brand relayed through one phone into a single device, which is the
    over-merge this package exists to prevent, arrived at through the rule meant to
    prevent it. Detection therefore groups those sources by their writer instead and
    keeps the host's model as provenance on the device (``device.host_model_raw``).

    Returns the host model when that case applies, else None. Deliberately narrow: it
    fires only on a relay route, only for a third-party writer, and only when the model
    names a phone. Firing wrongly costs an extra device that someone merges; not firing
    costs a merge nobody can see, so the doubt resolves toward firing.
    """
    provider_value = getattr(provider, "value", provider)
    if provider_value not in _WRITER_ID_ROUTES or not device_model or not writer_id:
        return None
    if not _is_writer_id(provider_value, writer_id):
        return None
    if _is_platform_own_writer(provider_value, writer_id, device_model):
        return None
    if infer_device_type_from_model(device_model) is not DeviceType.PHONE:
        return None
    return device_model


def _is_writer_id(provider_value: str, source: str) -> bool:
    """Whether `source` names an app that wrote the data, rather than the integration."""
    if provider_value not in _WRITER_ID_ROUTES:
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

    claims: list[IdentityClaim | None] = []
    # Suppressed when the model names the phone that relayed the data rather than the
    # device that recorded it: see relaying_host_model for why that string is not
    # evidence about this device, and detection._resolve for what groups those sources
    # instead.
    if relaying_host_model(provider, device_model, source) is None:
        claims.append(_claim(provider, DeviceIdentityKind.MODEL_STRING, device_model, IdentityConfidence.WEAK))

    # A Health Connect / HealthKit writer id, which only these routes carry. Elsewhere
    # `source` is the provider's own literal ("garmin", "google_health_api") and says
    # nothing about a device, so claiming it would give every one of that user's
    # devices on that route the same identity value - noise at best, and a grouping
    # key that means "same provider" if it were ever promoted.
    if source and _is_writer_id(provider_value, source):
        kind = (
            DeviceIdentityKind.HEALTH_CONNECT_PACKAGE
            if provider_value in _ANDROID_ROUTES
            else DeviceIdentityKind.HEALTHKIT_BUNDLE
        )
        claims.append(_claim(provider, kind, source, IdentityConfidence.WEAK))

    return [c for c in claims if c is not None]
