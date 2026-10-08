"""How a connected account's data is filed against physical devices."""

from enum import StrEnum


class DeviceAttribution(StrEnum):
    """Whether an account carries one device's data or several.

    Derived from ``user_connection.sensor_label`` rather than stored beside it, so
    the two can never disagree: naming the unit is what makes an account a single
    device, and clearing the name is what makes it per-record again.
    """

    # Each record is filed under the device its own metadata names - the watch,
    # head unit or app a Strava activity was recorded by. The account's
    # ``device_label`` fills in only where a record names nothing. The default, and
    # the only reading of an aggregator that carries several devices' data.
    PER_RECORD = "per_record"
    # Every record on the account is one declared unit (``sensor_label``), such as
    # the chest strap a gold-standard account's workouts were all recorded with.
    # What each record named is kept as the recorder that carried it.
    SINGLE_DEVICE = "single_device"
