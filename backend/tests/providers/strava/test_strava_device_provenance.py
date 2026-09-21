"""Tests for deriving the recording device from Strava activity metadata.

The cases below are written from the standpoint that gets this wrong in production:
one user holding several Strava accounts, each paired to a different wearable. The
failure that matters is not "we could not name the device" - that is a normal resting
state - but "we named the wrong one", or "we produced a value that quietly pools two
units into one".
"""

from typing import Any

import pytest

from app.schemas.enums import DeviceIdentityKind, DeviceType, IdentityConfidence
from app.schemas.providers.strava import ActivityJSON as StravaActivityJSON
from app.services.devices.identity import claims_from_strava_activity
from app.services.providers.strava.device_provenance import (
    PowerSource,
    RecordingMode,
    derive_provenance,
    upload_prefix,
)


def activity(**overrides: Any) -> StravaActivityJSON:
    base: dict[str, Any] = {
        "id": 12345,
        "name": "Evening Ride",
        "type": "Ride",
        "sport_type": "Ride",
        "start_date": "2024-01-15T18:00:00Z",
        "elapsed_time": 3600,
    }
    base.update(overrides)
    return StravaActivityJSON(**base)


class TestDeviceModel:
    """Only a name Strava reported for the hardware may become a device model."""

    def test_reported_device_name_becomes_the_model(self) -> None:
        provenance = derive_provenance(activity(device_name="Garmin Forerunner 965"))

        assert provenance.device_model == "Garmin Forerunner 965"
        assert provenance.brand == "Garmin"
        assert provenance.device_type is DeviceType.WATCH

    def test_absent_device_name_yields_none_not_empty_string(self) -> None:
        """An empty string reads as a device model and suppresses the label fallback.

        ``DataSourceRepository.ensure_data_source`` fills device_model from the
        connection's ``device_label`` only when the provider passed None. With "" the
        fallback never fires, which is the whole mechanism for naming the hardware
        behind a Strava account whose uploads carry no device.
        """
        assert derive_provenance(activity()).device_model is None

    def test_upload_source_never_becomes_a_device_model(self) -> None:
        """ "garmin_connect" is a sync channel; one athlete's Edge and watch share it."""
        provenance = derive_provenance(activity(external_id="garmin_push_1234567890"))

        assert provenance.upload_source == "garmin_connect"
        assert provenance.brand == "Garmin"
        assert provenance.device_model is None

    def test_website_upload_is_not_treated_as_hardware(self) -> None:
        """A hand-uploaded file could have come off anything, so it names nothing."""
        provenance = derive_provenance(activity(device_name="Strava GPX", external_id="track(1).gpx"))

        assert provenance.device_model is None
        assert provenance.brand is None
        assert provenance.recording is RecordingMode.FILE_UPLOAD

    def test_strava_mobile_app_is_a_real_recorder(self) -> None:
        provenance = derive_provenance(activity(device_name="Strava iPhone App", start_latlng=[52.2, 21.0]))

        assert provenance.device_model == "Strava iPhone App"
        assert provenance.device_type is DeviceType.PHONE
        assert provenance.recording is RecordingMode.PHONE
        assert "gps" in provenance.sensors


class TestDataSourceLabel:
    """The value that keys the data source, where a wrong one splits or merges rows."""

    def test_unidentified_upload_keeps_the_provider_literal(self) -> None:
        """Rows ingested before this feature must keep their key, or they duplicate."""
        assert derive_provenance(activity()).data_source_label == "strava"

    def test_mobile_app_label_does_not_collide_with_the_provider_literal(self) -> None:
        """ "Strava" differs from "strava" only by case, and would key a second row."""
        label = derive_provenance(activity(device_name="Strava Android App")).data_source_label

        assert label == "Strava App"
        assert label.casefold() != "strava"

    def test_uploader_becomes_the_label_when_identified(self) -> None:
        assert derive_provenance(activity(external_id="garmin_push_99")).data_source_label == "Garmin Connect"


class TestPowerProvenance:
    """Strava fills average_watts from its own estimate as readily as from a meter."""

    def test_device_watts_marks_a_real_power_meter(self) -> None:
        provenance = derive_provenance(activity(device_watts=True, average_watts=210.0))

        assert provenance.power_source is PowerSource.METER
        assert provenance.is_measured_power
        assert "power" in provenance.sensors

    def test_watts_without_device_watts_is_strava_s_own_estimate(self) -> None:
        provenance = derive_provenance(activity(average_watts=210.0, device_watts=False))

        assert provenance.power_source is PowerSource.ESTIMATED
        assert not provenance.is_measured_power
        # Not claimed as a sensor: nothing measured it.
        assert "power" not in provenance.sensors

    def test_no_power_at_all(self) -> None:
        assert derive_provenance(activity()).power_source is PowerSource.NONE


class TestRecordingMode:
    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"manual": True}, RecordingMode.MANUAL),
            ({"sport_type": "VirtualRide"}, RecordingMode.VIRTUAL),
            ({"external_id": "zwift-activity-1.fit"}, RecordingMode.VIRTUAL),
            ({"device_name": "Garmin Edge 840"}, RecordingMode.DEVICE),
            ({"external_id": "workout.tcx"}, RecordingMode.FILE_UPLOAD),
            ({}, RecordingMode.UNKNOWN),
        ],
    )
    def test_modes(self, overrides: dict[str, Any], expected: RecordingMode) -> None:
        assert derive_provenance(activity(**overrides)).recording is expected

    def test_a_trainer_ride_on_a_head_unit_is_still_that_head_unit(self) -> None:
        """`trainer` is checked last: the recorder outranks where the wheel was."""
        provenance = derive_provenance(activity(trainer=True, device_name="Wahoo ELEMNT BOLT"))

        assert provenance.recording is RecordingMode.DEVICE
        assert provenance.device_model == "Wahoo ELEMNT BOLT"

    def test_manual_entry_beats_every_other_signal(self) -> None:
        provenance = derive_provenance(activity(manual=True, device_name="Garmin Forerunner 965"))

        assert provenance.recording is RecordingMode.MANUAL


class TestUnmappedUploadSources:
    """Strava's upload names are undocumented, so the gaps have to be findable."""

    def test_an_unrecognised_upload_records_its_prefix(self) -> None:
        provenance = derive_provenance(activity(external_id="somevendor_55512.fit"))

        assert provenance.brand is None
        assert provenance.evidence["upload_source_prefix_unmapped"] == "somevendor"

    def test_a_file_that_names_its_brand_is_not_flagged_as_unmapped(self) -> None:
        """Wahoo names its files after the head unit, so the brand is recoverable."""
        provenance = derive_provenance(activity(external_id="2024-01-15-090000-ELEMNT BOLT-19-0.fit"))

        assert provenance.brand == "Wahoo"
        assert "upload_source_prefix_unmapped" not in provenance.evidence

    @pytest.mark.parametrize(
        ("external_id", "expected"),
        [
            ("garmin_push_1234567890", "garmin_push"),
            ("zwift-activity-99.fit", "zwift_activity"),
            ("track(1).gpx", "track"),
            ("12345678", None),
            ("", None),
            (None, None),
        ],
    )
    def test_upload_prefix(self, external_id: str | None, expected: str | None) -> None:
        assert upload_prefix(external_id) == expected


class TestSensors:
    def test_capabilities_are_read_from_the_fields_that_carry_them(self) -> None:
        provenance = derive_provenance(
            activity(
                has_heartrate=True,
                device_watts=True,
                average_cadence=88.0,
                average_temp=19,
                start_latlng=[52.2, 21.0],
            )
        )

        assert provenance.sensors == frozenset({"heart_rate", "power", "cadence", "temperature", "gps"})

    def test_an_empty_position_list_is_not_a_gps_fix(self) -> None:
        assert "gps" not in derive_provenance(activity(start_latlng=[], end_latlng=[])).sensors


class TestIdentityClaims:
    """The claims are evidence. None of them may group two units on its own."""

    def test_upload_source_claim_is_weak_and_scoped_to_the_athlete(self) -> None:
        claims = claims_from_strava_activity("Garmin Forerunner 965", "garmin_connect", "4242")

        upload = next(c for c in claims if c.kind is DeviceIdentityKind.STRAVA_UPLOAD_SOURCE)
        assert upload.confidence is IdentityConfidence.WEAK
        assert upload.route == "strava"
        assert "4242" in upload.value

    def test_two_accounts_uploading_the_same_way_do_not_share_a_claim(self) -> None:
        """The exact multi-account case: two Strava logins, both syncing via Garmin.

        Unscoped, both accounts' claims resolve to one device row and two units'
        data pools with no symptom. Scoped, they read as two, which a person merges.
        """
        first = claims_from_strava_activity(None, "garmin_connect", "1111")
        second = claims_from_strava_activity(None, "garmin_connect", "2222")

        assert first[0].value != second[0].value

    def test_no_upload_source_means_no_claims(self) -> None:
        assert claims_from_strava_activity(None, None, "4242") == []

    def test_device_name_is_claimed_as_a_model_not_a_unit(self) -> None:
        claims = claims_from_strava_activity("Garmin Forerunner 965", "garmin_connect", None)

        model = next(c for c in claims if c.kind is DeviceIdentityKind.MODEL_STRING)
        assert model.value == "Garmin Forerunner 965"
        assert model.confidence is IdentityConfidence.WEAK
