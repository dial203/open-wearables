"""Device type inference, including chest-strap detection (pure, no DB)."""

import pytest

from app.schemas.enums import DeviceType, infer_device_type_from_model, infer_device_type_from_source_name


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
        # EEG headbands, checked before the generic keyword pass: "Muse S Headband"
        # would otherwise be swallowed by the "band" substring and filed as a wristband.
        ("Muse S Athena", DeviceType.EEG),
        ("Muse", DeviceType.EEG),
        ("Muse 2", DeviceType.EEG),
        ("Dreem 3", DeviceType.EEG),
        ("Some EEG recorder", DeviceType.EEG),
        # A headband with no named modality is a headband, not an EEG.
        ("Forehead Headband", DeviceType.HEADBAND),
        # Handset model codes. A relayed stream is recognised by its model naming a
        # phone, so a Pixel or Galaxy reading as OTHER would leave every Android app on
        # it grouped as one device.
        ("Pixel 9 Pro", DeviceType.PHONE),
        ("SM-S901U", DeviceType.PHONE),
        ("Galaxy S22", DeviceType.PHONE),
        ("LM-V350", DeviceType.PHONE),
        # ...but the wearables sharing those prefixes are still wearables.
        ("Google Pixel Watch 4 (45mm)", DeviceType.WATCH),
        ("SM-R830", DeviceType.OTHER),
        ("SM-Q501", DeviceType.OTHER),
        ("Something Unrecognised", DeviceType.OTHER),
    ],
)
def test_infer_device_type_from_model(model: str | None, expected: DeviceType) -> None:
    assert infer_device_type_from_model(model) == expected


def test_chest_strap_ranks_above_watch_by_default() -> None:
    from app.schemas.enums.device_type import DEFAULT_DEVICE_TYPE_PRIORITY

    assert DEFAULT_DEVICE_TYPE_PRIORITY[DeviceType.CHEST_STRAP] < DEFAULT_DEVICE_TYPE_PRIORITY[DeviceType.WATCH]
    # EEG is the reference standard for sleep staging, as ECG is for beat-to-beat HR.
    assert DEFAULT_DEVICE_TYPE_PRIORITY[DeviceType.EEG] < DEFAULT_DEVICE_TYPE_PRIORITY[DeviceType.CHEST_STRAP]
    # The two types added after the table was first seeded take numbers no existing row
    # holds: initialize_defaults only inserts what is missing, so a renumbering here
    # would reach fresh databases only and tie with a row it did not rewrite.
    assert len(set(DEFAULT_DEVICE_TYPE_PRIORITY.values())) == len(DEFAULT_DEVICE_TYPE_PRIORITY)
    # every type has a priority so nothing silently falls back to the 99 sentinel
    assert set(DEFAULT_DEVICE_TYPE_PRIORITY) == set(DeviceType)


@pytest.mark.parametrize(
    ("source_name", "expected"),
    [
        # HealthKit names the device, not the app: the watch is identifiable even
        # when the payload carried no productType to infer a model from.
        ("Ali's Apple Watch", DeviceType.WATCH),
        ("Apple Watch", DeviceType.WATCH),
        ("Galaxy Watch7", DeviceType.WATCH),
        ("Ali's iPhone", DeviceType.PHONE),
        ("Ultrahuman Ring Air", DeviceType.RING),
        # Existing behaviour is unchanged.
        ("AutoSleep", DeviceType.WATCH),
        ("Mi Band 8", DeviceType.BAND),
        ("Oura", DeviceType.RING),
        ("Zepp Life", DeviceType.UNKNOWN),
        ("Huawei Health", DeviceType.UNKNOWN),
        # An app name is not a device — it must not be promoted out of UNKNOWN.
        ("Strava", DeviceType.UNKNOWN),
        ("Peloton", DeviceType.UNKNOWN),
        (None, DeviceType.UNKNOWN),
        ("", DeviceType.UNKNOWN),
    ],
)
def test_infer_device_type_from_source_name(source_name: str | None, expected: DeviceType) -> None:
    assert infer_device_type_from_source_name(source_name) == expected
