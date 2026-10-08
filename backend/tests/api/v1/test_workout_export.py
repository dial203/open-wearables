"""CSV exports of workouts.

What these pin down is the attribution and the grid, because neither can be checked
once the file has left OW: a column filed under the wrong device, or a second shifted
by one, looks exactly like a measurement.
"""

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import DataSource, SeriesTypeDefinition
from app.schemas.enums import ProviderName, SeriesType, get_series_type_id
from app.schemas.utils import SourceMetadata
from app.services.workout_export_service import overlap_groups, source_label
from tests.factories import (
    ApiKeyFactory,
    DataPointSeriesFactory,
    DataSourceFactory,
    EventRecordFactory,
    UserFactory,
    WorkoutDetailsFactory,
)
from tests.utils import api_key_headers

T0 = datetime(2026, 9, 14, 13, 0, 0, tzinfo=timezone.utc)


def _series(db: Session, series_type: SeriesType) -> SeriesTypeDefinition:
    definition = db.get(SeriesTypeDefinition, get_series_type_id(series_type))
    assert definition is not None
    return definition


def _sample(
    db: Session, data_source: DataSource, at: datetime, value: float, series_type: SeriesType = SeriesType.heart_rate
) -> None:
    DataPointSeriesFactory(data_source=data_source, series_type=_series(db, series_type), recorded_at=at, value=value)


def _label(data_source: DataSource) -> str:
    return source_label(SourceMetadata.from_data_source(data_source))


def _get(client: TestClient, path: str, **params: object) -> tuple[list[list[str]], dict[str, str]]:
    response = client.get(path, headers=api_key_headers(ApiKeyFactory().plain_key), params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(response.text))), dict(response.headers)


def _samples_csv(client: TestClient, user_id: object, workout_id: object, **params: object) -> list[list[str]]:
    rows, _ = _get(client, f"/api/v1/users/{user_id}/events/workouts/{workout_id}/export", **params)
    return rows


def _two_device_session(db: Session) -> tuple[object, DataSource, DataSource, object]:
    """A 10-second workout on a Garmin, with an Apple Watch worn alongside it."""
    user = UserFactory()
    garmin = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin", device_model="Forerunner 965")
    apple = DataSourceFactory(user=user, provider=ProviderName.APPLE, device_model="Watch7,1")
    workout = EventRecordFactory(
        data_source=garmin,
        category="workout",
        type_="running",
        start_datetime=T0,
        end_datetime=T0 + timedelta(seconds=10),
        duration_seconds=10,
    )
    for second in range(11):
        _sample(db, garmin, T0 + timedelta(seconds=second), 120 + second)
    # The watch writes every ~5 s, off the whole second.
    _sample(db, apple, T0 + timedelta(seconds=2, milliseconds=400), 118)
    _sample(db, apple, T0 + timedelta(seconds=7, milliseconds=900), 126)
    return user, garmin, apple, workout


class TestWideLayout:
    def test_devices_worn_together_line_up_on_one_second_grid(self, client: TestClient, db: Session) -> None:
        user, garmin, apple, workout = _two_device_session(db)

        rows = _samples_csv(client, user.id, workout.id)

        header, body = rows[0], rows[1:]
        # The workout's own device comes first: the HR validity tool takes the first
        # device column of an aligned file as its default criterion.
        assert header == [
            "timestamp_utc",
            f"{_label(garmin)} | heart_rate (bpm)",
            f"{_label(apple)} | heart_rate (bpm)",
        ]
        # Every second of the window, inclusive of the end.
        assert len(body) == 11
        assert body[0] == ["2026-09-14T13:00:00Z", "120", ""]
        # A sample goes to the second it was recorded in, truncated rather than rounded.
        assert body[2] == ["2026-09-14T13:00:02Z", "122", "118"]
        assert body[7] == ["2026-09-14T13:00:07Z", "127", "126"]
        # An empty cell is a second without a sample, never a zero.
        assert body[3][2] == ""
        assert all(row[2] in ("", "118", "126") for row in body)

    def test_a_dropout_stays_in_the_file_as_empty_rows(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        workout = EventRecordFactory(
            data_source=source, start_datetime=T0, end_datetime=T0 + timedelta(seconds=5), duration_seconds=5
        )
        _sample(db, source, T0, 100)
        _sample(db, source, T0 + timedelta(seconds=5), 105)

        body = _samples_csv(client, user.id, workout.id)[1:]

        assert [row[1] for row in body] == ["100", "", "", "", "", "105"]

    def test_several_samples_in_one_second_are_averaged_and_counted(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.POLAR)
        workout = EventRecordFactory(
            data_source=source, start_datetime=T0, end_datetime=T0 + timedelta(seconds=1), duration_seconds=1
        )
        _sample(db, source, T0 + timedelta(milliseconds=100), 100)
        _sample(db, source, T0 + timedelta(milliseconds=600), 103)
        _sample(db, source, T0 + timedelta(seconds=1), 104)

        rows = _samples_csv(client, user.id, workout.id)

        label = f"{_label(source)} | heart_rate (bpm)"
        assert rows[0] == ["timestamp_utc", label, f"{label} n"]
        assert rows[1] == ["2026-09-14T13:00:00Z", "101.5", "2"]
        assert rows[2] == ["2026-09-14T13:00:01Z", "104", "1"]

    def test_beat_intervals_stay_off_the_grid(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        strap = DataSourceFactory(user=user, provider=ProviderName.POLAR, device_model="H10")
        workout = EventRecordFactory(
            data_source=strap, start_datetime=T0, end_datetime=T0 + timedelta(seconds=2), duration_seconds=2
        )
        _sample(db, strap, T0, 75)
        for beat, ms in enumerate((0, 810, 1620)):
            _sample(db, strap, T0 + timedelta(milliseconds=ms), 810 + beat, SeriesType.rr_interval)

        wide = _samples_csv(client, user.id, workout.id)
        only_rr = _samples_csv(client, user.id, workout.id, types="rr_interval")
        long = _samples_csv(client, user.id, workout.id, layout="long")

        assert wide[0] == ["timestamp_utc", f"{_label(strap)} | heart_rate (bpm)"]
        # Asking the grid for beat intervals alone reads nothing, not everything.
        assert only_rr[0] == ["timestamp_utc"]
        assert sorted(row[4] for row in long[1:]) == ["heart_rate", "rr_interval", "rr_interval", "rr_interval"]

    def test_types_narrows_the_metrics(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        workout = EventRecordFactory(
            data_source=source, start_datetime=T0, end_datetime=T0 + timedelta(seconds=1), duration_seconds=1
        )
        _sample(db, source, T0, 140)
        _sample(db, source, T0, 3.2, SeriesType.speed)
        _sample(db, source, T0, 250, SeriesType.power)

        everything = _samples_csv(client, user.id, workout.id)
        heart_rate = _samples_csv(client, user.id, workout.id, types="heart_rate")

        label = _label(source)
        # Heart rate leads; the rest follow in type order.
        assert everything[0] == [
            "timestamp_utc",
            f"{label} | heart_rate (bpm)",
            f"{label} | power (watts)",
            f"{label} | speed (m_per_s)",
        ]
        assert heart_rate[0] == ["timestamp_utc", f"{label} | heart_rate (bpm)"]


class TestWindowAndOwnership:
    def test_only_the_workout_window_is_read_unless_padded(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        workout = EventRecordFactory(
            data_source=source, start_datetime=T0, end_datetime=T0 + timedelta(seconds=2), duration_seconds=2
        )
        _sample(db, source, T0 - timedelta(seconds=3), 90)
        _sample(db, source, T0 + timedelta(seconds=1), 100)
        _sample(db, source, T0 + timedelta(seconds=5), 110)

        plain = _samples_csv(client, user.id, workout.id, layout="long")
        padded = _samples_csv(client, user.id, workout.id, layout="long", pad_seconds=5)

        assert [row[5] for row in plain[1:]] == ["100"]
        assert [row[5] for row in padded[1:]] == ["90", "100", "110"]
        # elapsed_s counts from the workout's start, so a padded sample is negative.
        assert padded[1][1] == "-3.000"

    def test_daily_totals_and_other_users_are_left_out(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        stranger_source = DataSourceFactory(user=UserFactory(), provider=ProviderName.GARMIN)
        workout = EventRecordFactory(
            data_source=source, start_datetime=T0, end_datetime=T0 + timedelta(seconds=2), duration_seconds=2
        )
        _sample(db, source, T0, 100)
        _sample(db, stranger_source, T0, 150)
        DataPointSeriesFactory(
            data_source=source,
            series_type=_series(db, SeriesType.steps),
            recorded_at=T0 + timedelta(seconds=1),
            value=9000,
            is_daily_total=True,
        )

        long = _samples_csv(client, user.id, workout.id, layout="long")

        assert [(row[4], row[5]) for row in long[1:]] == [("heart_rate", "100")]

    def test_another_users_workout_is_not_found(self, client: TestClient, db: Session) -> None:
        _user, _garmin, _apple, workout = _two_device_session(db)

        response = client.get(
            f"/api/v1/users/{UserFactory().id}/events/workouts/{workout.id}/export",
            headers=api_key_headers(ApiKeyFactory().plain_key),
        )

        assert response.status_code == 404

    def test_an_empty_window_still_exports_and_says_so(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.WHOOP)
        workout = EventRecordFactory(
            data_source=source, start_datetime=T0, end_datetime=T0 + timedelta(seconds=1), duration_seconds=1
        )

        rows, headers = _get(client, f"/api/v1/users/{user.id}/events/workouts/{workout.id}/export")

        assert headers["x-export-row-count"] == "0"
        assert headers["x-export-source-count"] == "0"
        assert rows == [["timestamp_utc"], ["2026-09-14T13:00:00Z"], ["2026-09-14T13:00:01Z"]]


class TestRelayedCopies:
    def test_one_device_reached_two_ways_exports_once_unless_asked(self, client: TestClient, db: Session) -> None:
        """A ring read from its maker and relayed through Apple Health is one device.

        Exported as two, it would agree with itself perfectly - a validation result
        produced by the plumbing.
        """
        user = UserFactory()
        direct = DataSourceFactory(
            user=user, provider="oura", device_model="Oura Ring Gen3", source="oura_api", original_source_name="Oura"
        )
        relayed = DataSourceFactory(
            user=user, provider="apple", device_model=None, source="com.ouraring.oura", original_source_name="Oura"
        )
        workout = EventRecordFactory(
            data_source=direct, start_datetime=T0, end_datetime=T0 + timedelta(seconds=2), duration_seconds=2
        )
        _sample(db, direct, T0, 98)
        _sample(db, relayed, T0 + timedelta(seconds=1), 98)
        db.commit()

        deduplicated = _samples_csv(client, user.id, workout.id, layout="long")
        everything = _samples_csv(client, user.id, workout.id, layout="long", include_redundant_relays="true")

        source_column = deduplicated[0].index("data_source_id")
        assert {row[source_column] for row in deduplicated[1:]} == {str(direct.id)}
        assert {row[source_column] for row in everything[1:]} == {str(direct.id), str(relayed.id)}


class TestLongLayout:
    def test_every_sample_keeps_its_own_attribution(self, client: TestClient, db: Session) -> None:
        user, garmin, apple, workout = _two_device_session(db)

        rows, headers = _get(client, f"/api/v1/users/{user.id}/events/workouts/{workout.id}/export", layout="long")

        header, body = rows[0], rows[1:]
        column = {name: index for index, name in enumerate(header)}
        assert len(body) == 13
        assert headers["x-export-row-count"] == "13"
        assert headers["x-export-source-count"] == "2"
        watch_rows = [row for row in body if row[column["data_source_id"]] == str(apple.id)]
        assert [row[column["timestamp_utc"]] for row in watch_rows] == [
            "2026-09-14T13:00:02.400Z",
            "2026-09-14T13:00:07.900Z",
        ]
        assert watch_rows[0][column["elapsed_s"]] == "2.400"
        assert watch_rows[0][column["provider"]] == "apple"
        assert watch_rows[0][column["series_label"]] == f"{_label(apple)} | heart_rate (bpm)"
        assert {row[column["data_source_id"]] for row in body} == {str(garmin.id), str(apple.id)}

    def test_filename_names_participant_activity_and_local_date(self, client: TestClient, db: Session) -> None:
        user = UserFactory(external_user_id="P-07")
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        workout = EventRecordFactory(
            data_source=source,
            type_="running",
            # 23:30 UTC is already the next day in UTC+02:00.
            start_datetime=datetime(2026, 9, 14, 23, 30, tzinfo=timezone.utc),
            end_datetime=datetime(2026, 9, 14, 23, 31, tzinfo=timezone.utc),
            duration_seconds=60,
            zone_offset="+02:00",
        )

        _, headers = _get(client, f"/api/v1/users/{user.id}/events/workouts/{workout.id}/export")

        assert headers["content-disposition"] == 'attachment; filename="P_07-running-20260915-0130-wide.csv"'


class TestSummaryExport:
    def test_one_row_per_workout_with_overlaps_grouped(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        garmin = DataSourceFactory(user=user, provider=ProviderName.GARMIN, source="garmin")
        whoop = DataSourceFactory(user=user, provider=ProviderName.WHOOP, source="whoop")
        run_garmin = EventRecordFactory(
            data_source=garmin, start_datetime=T0, end_datetime=T0 + timedelta(minutes=40), duration_seconds=2400
        )
        WorkoutDetailsFactory(event_record=run_garmin, heart_rate_avg=Decimal("151.25"))
        run_whoop = EventRecordFactory(
            data_source=whoop,
            start_datetime=T0 + timedelta(minutes=1),
            end_datetime=T0 + timedelta(minutes=41),
            duration_seconds=2400,
        )
        # No provider average: OW works one out from the workout's own samples.
        _sample(db, whoop, T0 + timedelta(minutes=2), 140)
        _sample(db, whoop, T0 + timedelta(minutes=3), 150)
        later = EventRecordFactory(
            data_source=garmin,
            start_datetime=T0 + timedelta(hours=3),
            end_datetime=T0 + timedelta(hours=4),
            duration_seconds=3600,
        )

        rows, headers = _get(
            client,
            f"/api/v1/users/{user.id}/events/workouts/export",
            start_date="2026-09-14T00:00:00Z",
            end_date="2026-09-15T00:00:00Z",
        )

        header, body = rows[0], rows[1:]
        column = {name: index for index, name in enumerate(header)}
        assert headers["x-export-row-count"] == "3"
        by_id = {row[column["workout_id"]]: row for row in body}
        assert [row[column["workout_id"]] for row in body] == [str(run_garmin.id), str(run_whoop.id), str(later.id)]
        assert by_id[str(run_garmin.id)][column["overlap_group"]] == "1"
        assert by_id[str(run_whoop.id)][column["overlap_group"]] == "1"
        assert by_id[str(later.id)][column["overlap_group"]] == "2"
        # The provider's own figure, unrounded.
        assert by_id[str(run_garmin.id)][column["avg_hr_bpm"]] == "151.25"
        assert by_id[str(run_garmin.id)][column["avg_hr_origin"]] == "provider"
        assert by_id[str(run_whoop.id)][column["avg_hr_bpm"]] == "145"
        assert by_id[str(run_whoop.id)][column["avg_hr_origin"]] == "computed_from_samples"
        assert by_id[str(later.id)][column["avg_hr_origin"]] == ""
        assert by_id[str(run_whoop.id)][column["provider"]] == "whoop"
        assert by_id[str(run_whoop.id)][column["data_source_id"]] == str(whoop.id)

    def test_time_in_zone_flattens_to_one_column_per_zone(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        source = DataSourceFactory(user=user, provider=ProviderName.GARMIN)
        workout = EventRecordFactory(
            data_source=source, start_datetime=T0, end_datetime=T0 + timedelta(minutes=20), duration_seconds=1200
        )
        WorkoutDetailsFactory(
            event_record=workout,
            hr_zones={"zones": [{"zone": 1, "seconds": 600.0}, {"zone": 3, "seconds": 240.5}]},
        )

        rows, _ = _get(
            client,
            f"/api/v1/users/{user.id}/events/workouts/export",
            start_date="2026-09-14T00:00:00Z",
            end_date="2026-09-15T00:00:00Z",
        )

        column = {name: index for index, name in enumerate(rows[0])}
        assert "hr_zone_2_s" not in column
        assert rows[1][column["hr_zone_1_s"]] == "600"
        assert rows[1][column["hr_zone_3_s"]] == "240.5"


def test_overlap_groups_are_transitive_and_touching_spans_are_separate() -> None:
    a, b, c, d = (uuid4() for _ in range(4))
    hour = timedelta(hours=1)
    groups = overlap_groups(
        [
            (a, T0, T0 + hour),
            (b, T0 + hour / 2, T0 + 2 * hour),  # overlaps a
            (c, T0 + 1.5 * hour, T0 + 3 * hour),  # overlaps b only
            (d, T0 + 3 * hour, T0 + 4 * hour),  # starts as c ends
        ]
    )

    assert [groups[x] for x in (a, b, c, d)] == [1, 1, 1, 2]
