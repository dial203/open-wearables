"""Device type enum and priority configuration."""

from enum import StrEnum


class DeviceType(StrEnum):
    """Type of device that collected health data."""

    WATCH = "watch"
    BAND = "band"
    PHONE = "phone"
    SCALE = "scale"
    RING = "ring"
    TABLET = "tablet"
    CHEST_STRAP = "chest_strap"
    HR_SENSOR = "hr_sensor"
    HEADPHONES = "headphones"
    HEAD_MOUNTED = "head_mounted"
    GLASSES = "glasses"
    SMART_DISPLAY = "smart_display"
    BP_MONITOR = "bp_monitor"
    GLUCOSE_METER = "glucose_meter"
    THERMOMETER = "thermometer"
    SLEEP_MONITOR = "sleep_monitor"
    BIKE_COMPUTER = "bike_computer"
    FITNESS_MACHINE = "fitness_machine"
    # Fork-only. EEG is ranked for its modality (the reference for sleep staging);
    # HEADBAND is a head-worn device whose modality the model string does not name.
    EEG = "eeg"
    HEADBAND = "headband"
    OTHER = "other"
    UNKNOWN = "unknown"


# System-wide default device type priority (lower = higher priority)
# Used when user hasn't set custom priorities.
# EEG ranks first and chest straps second: EEG is the reference standard for sleep
# staging and an ECG chest strap for beat-to-beat heart rate, both ahead of the
# optical wrist and finger sensors that infer the same quantities.
#
# Existing numbers are never renumbered. DeviceTypePriorityRepository.initialize_defaults
# is additive: it inserts only the types missing from an already-seeded database and
# never rewrites a row an operator may have customised. Renumbering here would apply to
# fresh databases only, and a new type inserted at a number an existing row already
# holds would tie with it, with nothing to break the tie. So EEG took 0 and HEADBAND 8
# when they were added, and upstream's extended types (#1729) take 9 and up: below every
# wearable type the fork already ranked, above unknown, each on its own number. Upstream
# puts them all at 6 alongside OTHER; that tie is what this ordering avoids.
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
    # Upstream's extended types, roughly by how often they carry a signal the
    # wearables above also measure: dedicated HR sensors and sleep monitors first,
    # then hosts and accessories that mostly relay or log.
    DeviceType.HR_SENSOR: 9,
    DeviceType.SLEEP_MONITOR: 10,
    DeviceType.HEAD_MOUNTED: 11,
    DeviceType.TABLET: 12,
    DeviceType.BIKE_COMPUTER: 13,
    DeviceType.FITNESS_MACHINE: 14,
    DeviceType.BP_MONITOR: 15,
    DeviceType.THERMOMETER: 16,
    DeviceType.GLUCOSE_METER: 17,
    DeviceType.HEADPHONES: 18,
    DeviceType.GLASSES: 19,
    DeviceType.SMART_DISPLAY: 20,
    DeviceType.UNKNOWN: 99,
}
