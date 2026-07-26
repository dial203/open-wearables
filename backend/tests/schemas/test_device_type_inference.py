"""Device type inference, including chest-strap detection (pure, no DB)."""

import pytest

from app.schemas.enums import DeviceType, infer_device_type_from_model


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # Chest straps (ECG) — the reference standard for HR / beat-to-beat data.
        ("Polar H10", DeviceType.CHEST_STRAP),
        ("H10", DeviceType.CHEST_STRAP),
        ("Polar H9", DeviceType.CHEST_STRAP),
        ("HRM 600", DeviceType.CHEST_STRAP),
        ("HRM-Pro", DeviceType.CHEST_STRAP),
        ("HRM-Dual", DeviceType.CHEST_STRAP),
        ("Garmin HRM-Fit", DeviceType.CHEST_STRAP),
        ("Wahoo TICKR", DeviceType.CHEST_STRAP),
        ("Some Chest Strap", DeviceType.CHEST_STRAP),
        # Optical arm bands must NOT be classified as chest straps.
        ("Polar Verity Sense", DeviceType.BAND),
        ("Polar OH1", DeviceType.BAND),
        # Existing behaviour is unchanged.
        ("Watch7,2", DeviceType.WATCH),
        ("iPhone18,1", DeviceType.PHONE),
        ("Apple Watch Ultra 3", DeviceType.WATCH),
        ("Forerunner 965", DeviceType.WATCH),
        ("Polar Grit X2 Pro", DeviceType.WATCH),
        ("Oura Ring Gen3", DeviceType.RING),
        ("Whoop 5.0", DeviceType.BAND),
        ("vivosmart 5", DeviceType.BAND),
        (None, DeviceType.UNKNOWN),
        ("", DeviceType.UNKNOWN),
        ("Something Unrecognised", DeviceType.OTHER),
    ],
)
def test_infer_device_type_from_model(model: str | None, expected: DeviceType) -> None:
    assert infer_device_type_from_model(model) == expected


def test_chest_strap_ranks_above_watch_by_default() -> None:
    from app.schemas.enums.device_type import DEFAULT_DEVICE_TYPE_PRIORITY

    assert DEFAULT_DEVICE_TYPE_PRIORITY[DeviceType.CHEST_STRAP] < DEFAULT_DEVICE_TYPE_PRIORITY[DeviceType.WATCH]
    # every type has a priority so nothing silently falls back to the 99 sentinel
    assert set(DEFAULT_DEVICE_TYPE_PRIORITY) == set(DeviceType)
