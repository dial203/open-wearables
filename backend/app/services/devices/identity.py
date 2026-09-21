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

import re
from dataclasses import dataclass
from typing import Any

from app.schemas.enums import (
    WRITER_MODEL_SEPARATOR,
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


def account_scoped_value(route: ProviderName | str, value: str, account_scope: str | None) -> str:
    """A claim value narrowed to one account, on the routes that need it.

    Same shape as an AGGREGATOR_WRITER_MODEL value, for the same reason: the bare key
    is ambiguous, and the thing that disambiguates it is appended rather than replacing
    it, so the model is still legible in the stored value.
    """
    route_value = getattr(route, "value", route)
    if not account_scope or route_value not in ACCOUNT_SCOPED_MODEL_ROUTES:
        return value
    return f"{value}{WRITER_MODEL_SEPARATOR}{account_scope}"


def grouping_claim(
    route: ProviderName | str,
    writer_id: str | None,
    model: str | None,
    account_scope: str | None = None,
    sensor_declared: bool = False,
) -> IdentityClaim | None:
    """The one claim that may group data sources within this route.

    On a maker's own API the model string names the unit that recorded the data, and
    grouping on it reproduces what the provider already asserts.

    On a relay route it does not: the platform reports the host that ran the writing
    app, so a Muse headband, an Oura ring and a WHOOP band relayed through one iPhone
    all report ``iPhone15,3``. Grouping on that pools them into one device - the
    over-merge this package exists to prevent, reached through the rule meant to
    prevent it.

    The key there is the writer and the host together, not the writer alone. The
    writer alone would group one app's streams across every host it ever synced
    through, so an Oura app relaying through a 2017 phone and a 2024 phone reads as
    one ring - and for a person who replaced the ring in between, that is two units
    merged with no symptom. The pair over-splits instead: two hosts read as two
    devices, a person sees both and merges them, and nothing was pooled while they
    decided.

    ``account_scope`` narrows the key to one connected account on the routes that need
    it; see ACCOUNT_SCOPED_MODEL_ROUTES. ``sensor_declared`` says a person has told us
    the reported model is the recorder rather than the unit - see relaying_host_model.
    """
    host_model = relaying_host_model(route, model, writer_id, sensor_declared)
    writer = writer_id.strip() if writer_id else None
    cleaned_model = model.strip() if model else None

    if host_model is not None and writer and cleaned_model:
        return _claim(
            route,
            DeviceIdentityKind.AGGREGATOR_WRITER_MODEL,
            account_scoped_value(route, f"{writer}{WRITER_MODEL_SEPARATOR}{cleaned_model}", account_scope),
            IdentityConfidence.WEAK,
        )
    # A declared sensor with nothing reported alongside it: there is no recorder to
    # record as the host, so the declared unit is simply the unit.
    key = writer if (sensor_declared and writer and not cleaned_model) else cleaned_model
    if key is None:
        return None
    return _claim(
        route,
        DeviceIdentityKind.MODEL_STRING,
        account_scoped_value(route, key, account_scope),
        IdentityConfidence.WEAK,
    )


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
        # The grouping key: the writer/model pair on a relay route, the bare model
        # everywhere else. See grouping_claim.
        grouping_claim(provider, writer_id, model),
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


def claims_from_strava_activity(
    device_name: str | None,
    upload_source: str | None,
    athlete_id: str | None = None,
) -> list[IdentityClaim]:
    """Claims from one Strava activity's provenance metadata.

    Strava carries other makers' data and keeps two traces of whose: ``device_name``,
    which names the recorder when the uploading vendor supplied it, and
    ``external_id``, from which
    ``app/services/providers/strava/device_provenance.py`` derives the upload source.

    ``device_name`` is already claimed as a MODEL_STRING by ``claims_from_data_source``
    off ``data_source.device_model``, so it is not repeated here. What this adds is the
    upload source, and it is deliberately WEAK and deliberately not a grouping kind.
    "garmin_connect" is a sync channel: an athlete's Edge and Forerunner both arrive
    through it, and grouping on it would pool two units into one with no symptom -
    the merge ``detection.py`` exists to prevent.

    The claim value is scoped by athlete where we know it, because a study running
    several Strava accounts is precisely the case where one user holds two connections
    that both upload via Garmin Connect. Unscoped, those two accounts' claims collide
    on a single device. Scoped, they read as two, which someone can merge - the
    recoverable direction.
    """
    if not upload_source:
        return []
    value = f"{upload_source}{WRITER_MODEL_SEPARATOR}{athlete_id}" if athlete_id else upload_source
    claim = _claim(
        ProviderName.STRAVA,
        DeviceIdentityKind.STRAVA_UPLOAD_SOURCE,
        value,
        IdentityConfidence.WEAK,
    )
    # device_name is echoed only when it would otherwise be lost: a source whose
    # device_model came from the connection's label still benefits from recording what
    # Strava itself reported, and the kind makes clear it describes a model, not a unit.
    model_claim = _claim(ProviderName.STRAVA, DeviceIdentityKind.MODEL_STRING, device_name, IdentityConfidence.WEAK)
    return [c for c in (claim, model_claim) if c is not None]


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
        # Health Connect stamps some rows with the OS itself rather than an app.
        "android",
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

# Routes where two of one user's accounts reporting the same model are more likely to
# be two units than one, so the model string groups only within an account.
#
# The within-route rule everywhere else - group on an exact model-string match -
# "reproduces what a provider already asserts". That assertion holds for one account.
# It stops holding the moment a user connects several: a validation study running a
# Strava account per wearable, with two devices of the same model, gets one device row
# and two units' data pooled into it. Pooling is the failure this package exists to
# prevent, and unlike a split it leaves no symptom.
#
# Scoping over-splits instead - one physical unit shared by two accounts reads as two
# devices, which a person merges. Deliberately not applied to every route: on a route
# where a user holds a single account the scope is a constant, so it would only churn
# existing claim values for no gain.
ACCOUNT_SCOPED_MODEL_ROUTES: frozenset[str] = frozenset({ProviderName.STRAVA.value})

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
    sensor_declared: bool = False,
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

    ``sensor_declared`` is the one case that skips every test below, on any route. A
    watch recording a chest strap is the same shape as a phone relaying a headband -
    the reported model is the recorder, the real instrument is something else - but no
    provider exposes it: Strava names the uploading watch and nothing at all about the
    strap paired to it, and an activity with has_heartrate looks identical either way.
    Nothing can be inferred, so a person declares it on the connection
    (``user_connection.sensor_label``), and a declaration is not second-guessed.
    """
    provider_value = getattr(provider, "value", provider)
    if not device_model or not writer_id:
        return None
    if sensor_declared:
        return device_model
    if provider_value not in WRITER_ID_ROUTES:
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
    account_scope: str | None = None,
    declared_sensor: str | None = None,
) -> list[IdentityClaim]:
    """Baseline claims derivable from what every data source already stores.

    This is the floor, not the ceiling: it runs for every ingest path so that a
    device always has at least the model-string claim the backfill seeded, and the
    route-specific extractors above add whatever else that route affords.

    ``account_scope`` identifies the connected account, for routes where one user
    holds several and a shared model string would pool them; see
    ACCOUNT_SCOPED_MODEL_ROUTES.

    ``declared_sensor`` is a unit a person has said was worn under whatever the
    provider named - a chest strap recorded through a watch. It takes the writer's
    place, because on this row it plays the writer's part exactly: the thing that
    produced the data, distinct from the hardware the provider reported.
    """
    provider_value = getattr(provider, "value", provider)

    # A Health Connect / HealthKit writer id, which only these routes carry. Elsewhere
    # `source` is the provider's own literal ("garmin", "google_health_api") and says
    # nothing about a device, so claiming it would give every one of that user's
    # devices on that route the same identity value - noise at best, and a grouping
    # key that means "same provider" if it were ever promoted.
    writer_id = source if source and _is_writer_id(provider_value, source) else None
    if declared_sensor:
        writer_id = declared_sensor

    # The grouping key. On a relay route the model names the host that ran the writing
    # app, so it is paired with the writer rather than claimed on its own; see
    # grouping_claim, and detection._resolve for what does the grouping.
    claims: list[IdentityClaim | None] = [
        grouping_claim(provider, writer_id, device_model, account_scope, bool(declared_sensor))
    ]

    if writer_id and not declared_sensor:
        kind = (
            DeviceIdentityKind.HEALTH_CONNECT_PACKAGE
            if provider_value in _ANDROID_ROUTES
            else DeviceIdentityKind.HEALTHKIT_BUNDLE
        )
        claims.append(_claim(provider, kind, writer_id, IdentityConfidence.WEAK))

    return [c for c in claims if c is not None]
