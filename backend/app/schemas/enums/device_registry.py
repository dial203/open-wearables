"""Enums for the device registry: identity claims, edit history and link proposals."""

from enum import StrEnum


class DeviceIdentityKind(StrEnum):
    """What kind of identifier a device identity claim holds.

    No identifier is comparable across ingest routes. The same physical Oura ring
    reports ``ring_configuration.id`` on the direct API, a HealthKit bundle id via
    Apple Health and a Health Connect package name via Google Health - three values
    with nothing in common. So a claim is always scoped to the route that issued it,
    and cross-route identity is established by evidence, never by matching values.
    """

    # Provider-issued, stable for the life of the unit.
    OURA_CONFIG_ID = "oura_config_id"  # /v2/usercollection/ring_configuration -> id
    HEALTHKIT_DEVICE_ID = "healthkit_device_id"  # HKDevice localIdentifier / udiDeviceIdentifier
    GARMIN_SUMMARY_PREFIX = "garmin_summary_prefix"  # leading token of Garmin summaryId

    # Writer identity: names the app, not the unit. Two rings owned by one user
    # share these, so they are never sufficient on their own.
    HEALTHKIT_BUNDLE = "healthkit_bundle"  # HKSource.bundleIdentifier
    HEALTH_CONNECT_PACKAGE = "health_connect_package"  # DataOrigin.packageName

    # Hardware descriptors: narrow the field, never identify a unit.
    APPLE_PRODUCT_TYPE = "apple_product_type"  # "Watch7,5"
    MODEL_STRING = "model_string"  # provider's own model / device name
    SERIAL = "serial"  # only where a provider genuinely exposes one


class IdentityConfidence(StrEnum):
    """How much weight an identity claim carries when grouping data sources.

    ``STRONG`` is a provider-issued identifier that is stable for the life of the
    unit and distinguishes two same-model devices owned by the same user; only
    these may group data sources automatically. ``WEAK`` identifies the writing
    app or the hardware model, which two identical units share, so it can support
    a proposal but never create a grouping on its own.
    """

    STRONG = "strong"
    WEAK = "weak"


# Kinds that may group data sources without a human confirming it. Everything else
# is corroborating evidence. Kept as data rather than a method on the enum so the
# ingest path and the proposal scorer read the same list.
STRONG_IDENTITY_KINDS: frozenset[DeviceIdentityKind] = frozenset(
    {
        DeviceIdentityKind.OURA_CONFIG_ID,
        DeviceIdentityKind.HEALTHKIT_DEVICE_ID,
        DeviceIdentityKind.GARMIN_SUMMARY_PREFIX,
        DeviceIdentityKind.SERIAL,
    }
)


class LabelSource(StrEnum):
    """Where a device's label came from. A manual label is never overwritten by detection."""

    AUTO = "auto"
    MANUAL = "manual"


class DeviceHistoryAction(StrEnum):
    """Every mutation recorded in device_history, append-only."""

    CREATED = "created"
    DETECTED = "detected"  # created by auto-detection rather than by hand
    UPDATED = "updated"  # label, type, notes, wear location
    LINKED = "linked"  # a data source attached to this device
    UNLINKED = "unlinked"
    MERGED = "merged"  # this device absorbed another
    SPLIT = "split"  # data sources moved out to a new device
    RETIRED = "retired"
    REACTIVATED = "reactivated"
    IDENTITY_ADDED = "identity_added"
    LINK_PROPOSED = "link_proposed"
    LINK_ACCEPTED = "link_accepted"
    LINK_REJECTED = "link_rejected"


class LinkProposalStatus(StrEnum):
    """Lifecycle of a cross-route link proposal.

    ``REJECTED`` rows are kept forever: they are what stops the scorer proposing
    the same pair again every time it runs.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
