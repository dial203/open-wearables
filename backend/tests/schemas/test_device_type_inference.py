"""Device type inference, including chest-strap detection (pure, no DB)."""

import pytest

from app.schemas.enums import (
    DeviceType,
    device_type_from_platform_report,
    infer_device_type_from_model,
    infer_device_type_from_source_name,
    reconcile_device_type,
)


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


class TestPlatformReportedDeviceType:
    """Health Connect declares a device type; HealthKit has no such field.

    The distinction matters because it is the only route that classifies hardware
    instead of leaving us to read a classification out of a model string - and on a
    relayed stream that model string names the phone.
    """

    @pytest.mark.parametrize(
        ("reported", "expected"),
        [
            ("watch", DeviceType.WATCH),
            ("ring", DeviceType.RING),
            ("phone", DeviceType.PHONE),
            ("scale", DeviceType.SCALE),
            ("chest_strap", DeviceType.CHEST_STRAP),
            # Health Connect's own spelling for a wrist band.
            ("fitness_band", DeviceType.BAND),
            ("smart_display", DeviceType.OTHER),
            # Case and padding, since the value is relayed through a mobile SDK.
            ("WATCH", DeviceType.WATCH),
            ("  ring  ", DeviceType.RING),
        ],
    )
    def test_the_platform_s_own_constants_map_across(self, reported: str, expected: DeviceType) -> None:
        assert device_type_from_platform_report(reported) is expected

    @pytest.mark.parametrize("reported", [None, "", "unknown", "UNKNOWN", "toaster", 7, object()])
    def test_nothing_declared_is_none_not_unknown(self, reported: object) -> None:
        """A writer that passed no Device has said nothing, which is not "unknown".

        Collapsing the two would let an unfilled field overwrite a type a good model
        string established.
        """
        assert device_type_from_platform_report(reported) is None

    def test_head_mounted_is_a_headband_not_an_eeg(self) -> None:
        """Health Connect says where a device sits, never what it measures.

        EEG outranks every other type because of the modality, and an optical
        forehead sensor is head-mounted too.
        """
        assert device_type_from_platform_report("head_mounted") is DeviceType.HEADBAND


class TestReconcileDeviceType:
    def test_the_platform_report_beats_inference(self) -> None:
        """Inference reads a string; the report is the writer naming its hardware."""
        assert reconcile_device_type(DeviceType.RING, DeviceType.PHONE) is DeviceType.RING

    def test_nothing_reported_leaves_inference_alone(self) -> None:
        assert reconcile_device_type(None, DeviceType.CHEST_STRAP) is DeviceType.CHEST_STRAP

    def test_a_reported_unknown_leaves_inference_alone(self) -> None:
        assert reconcile_device_type(DeviceType.UNKNOWN, DeviceType.WATCH) is DeviceType.WATCH

    def test_a_model_string_may_refine_head_mounted_to_eeg(self) -> None:
        """The one case where the guess adds what the platform has no field for."""
        assert reconcile_device_type(DeviceType.HEADBAND, DeviceType.EEG) is DeviceType.EEG

    def test_a_model_string_does_not_second_guess_a_reported_band(self) -> None:
        """The writer had CHEST_STRAP available and chose FITNESS_BAND.

        Overriding that would re-open classification to exactly the string matching
        the refinement map exists to bound.
        """
        assert reconcile_device_type(DeviceType.BAND, DeviceType.CHEST_STRAP) is DeviceType.BAND
