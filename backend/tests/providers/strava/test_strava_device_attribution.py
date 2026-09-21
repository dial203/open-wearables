"""Tests for how derived Strava provenance reaches the data source and device registry.

These cover the wiring rather than the derivation (see test_strava_device_provenance.py):
what ``_normalize_workout`` puts on the record, and whether the values it puts there
can be keyed, grouped and fallen back on correctly downstream.
"""

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.schemas.enums import DeviceIdentityKind
from app.schemas.providers.strava import ActivityJSON as StravaActivityJSON
from app.services.providers.strava.oauth import StravaOAuth
from app.services.providers.strava.workouts import StravaWorkouts


@pytest.fixture
def workouts() -> StravaWorkouts:
    oauth = StravaOAuth(
        user_repo=MagicMock(),
        connection_repo=MagicMock(),
        provider_name="strava",
        api_base_url="https://www.strava.com/api/v3",
    )
    return StravaWorkouts(
        workout_repo=MagicMock(),
        connection_repo=MagicMock(),
        provider_name="strava",
        api_base_url="https://www.strava.com/api/v3",
        oauth=oauth,
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


class TestRecordDeviceModel:
    def test_reported_device_reaches_the_record(self, workouts: StravaWorkouts) -> None:
        record, _ = workouts._normalize_workout(activity(device_name="Garmin Forerunner 965"), uuid4())

        assert record.device_model == "Garmin Forerunner 965"
        assert record.source_name == "Garmin Forerunner 965"

    def test_no_device_leaves_device_model_null(self, workouts: StravaWorkouts) -> None:
        """Null, not "". ensure_data_source fills device_model from the connection's
        device_label only when the provider passed None, and that fallback is the only
        way to name the hardware behind a Strava account whose uploads carry none.
        """
        record, _ = workouts._normalize_workout(activity(), uuid4())

        assert record.device_model is None

    def test_provider_is_set_explicitly(self, workouts: StravaWorkouts) -> None:
        """`source` may now be an uploader name, which provider inference would misread
        ("Garmin Connect" -> the Garmin provider), filing Strava data under Garmin.
        """
        record, _ = workouts._normalize_workout(activity(external_id="garmin_push_1"), uuid4())

        assert record.provider == "strava"
        assert record.source == "Garmin Connect"

    def test_unidentified_upload_keeps_the_original_source_key(self, workouts: StravaWorkouts) -> None:
        record, _ = workouts._normalize_workout(activity(), uuid4())

        assert record.source == "strava"

    def test_external_id_stays_the_activity_id(self, workouts: StravaWorkouts) -> None:
        """The webhook delete path resolves records by Strava activity id; the upload
        name is provenance and must not displace it.
        """
        record, _ = workouts._normalize_workout(activity(id=999, external_id="garmin_push_1"), uuid4())

        assert record.external_id == "999"


class TestRecordIdentityClaims:
    def test_upload_source_claim_rides_along_with_the_record(self, workouts: StravaWorkouts) -> None:
        record, _ = workouts._normalize_workout(
            activity(external_id="garmin_push_1", athlete={"id": 4242}),
            uuid4(),
        )

        kinds = {c.kind for c in (record.identity_claims or [])}
        assert DeviceIdentityKind.STRAVA_UPLOAD_SOURCE in kinds

    def test_claims_are_scoped_by_athlete(self, workouts: StravaWorkouts) -> None:
        """One user, two Strava accounts, both syncing through Garmin Connect."""
        user_id = uuid4()
        first, _ = workouts._normalize_workout(activity(external_id="garmin_push_1", athlete={"id": 1}), user_id)
        second, _ = workouts._normalize_workout(activity(external_id="garmin_push_2", athlete={"id": 2}), user_id)

        def upload_value(record: Any) -> str:
            return next(c.value for c in record.identity_claims if c.kind is DeviceIdentityKind.STRAVA_UPLOAD_SOURCE)

        assert upload_value(first) != upload_value(second)

    def test_missing_athlete_block_does_not_raise(self, workouts: StravaWorkouts) -> None:
        record, _ = workouts._normalize_workout(activity(external_id="garmin_push_1"), uuid4())

        assert record.identity_claims


class TestCadenceMetric:
    def test_average_cadence_is_stored(self, workouts: StravaWorkouts) -> None:
        _, detail = workouts._normalize_workout(activity(average_cadence=88.5), uuid4())

        assert detail.average_cadence is not None
        assert float(detail.average_cadence) == pytest.approx(88.5)


class TestBackfillEnrichment:
    """The list endpoint omits every field that says what recorded an activity."""

    def test_detail_is_fetched_and_replaces_the_summary(self, workouts: StravaWorkouts) -> None:
        summary = activity()
        detail_payload = {
            "id": 12345,
            "name": "Evening Ride",
            "type": "Ride",
            "sport_type": "Ride",
            "start_date": "2024-01-15T18:00:00Z",
            "elapsed_time": 3600,
            "device_name": "Garmin Forerunner 965",
            "external_id": "garmin_push_1",
        }
        workouts.get_workout_detail_from_api = MagicMock(return_value=detail_payload)  # type: ignore[method-assign]

        enriched = workouts._enrich_with_detail(MagicMock(), uuid4(), summary)

        assert enriched.device_name == "Garmin Forerunner 965"
        assert enriched.external_id == "garmin_push_1"

    def test_a_failed_fetch_falls_back_to_the_summary(self, workouts: StravaWorkouts) -> None:
        """An exhausted rate limit costs device attribution, never the import."""
        summary = activity()
        workouts.get_workout_detail_from_api = MagicMock(side_effect=RuntimeError("429"))  # type: ignore[method-assign]

        assert workouts._enrich_with_detail(MagicMock(), uuid4(), summary) is summary

    def test_an_unparseable_detail_falls_back_to_the_summary(self, workouts: StravaWorkouts) -> None:
        summary = activity()
        workouts.get_workout_detail_from_api = MagicMock(return_value={"id": "not-an-activity"})  # type: ignore[method-assign]

        assert workouts._enrich_with_detail(MagicMock(), uuid4(), summary) is summary

    def test_setting_off_makes_no_extra_request(self, workouts: StravaWorkouts) -> None:
        summary = activity()
        fetch = MagicMock()
        workouts.get_workout_detail_from_api = fetch  # type: ignore[method-assign]

        with patch(
            "app.services.providers.strava.workouts.settings",
            strava_enrich_activity_detail=False,
        ):
            assert workouts._enrich_with_detail(MagicMock(), uuid4(), summary) is summary

        fetch.assert_not_called()
