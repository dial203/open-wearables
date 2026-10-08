"""Two Strava activities recorded at the same time never share one series.

An activity's samples go to the data source its device name and upload channel
resolve to. Two activities with no device name from the same channel - two FIT files
uploaded by hand, say, or a backfill that could not fetch the detail - resolved to the
same source, and the second one's per-second heart rate overwrote the first's at every
shared second and filled its gaps. The run against the real database below is what
showed it: the stored trace alternated between the two recorders.
"""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import DataPointSeries, DataSource, EventRecord
from app.repositories.event_record_repository import EventRecordRepository
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.providers.strava import ActivityJSON as StravaActivityJSON
from app.services.providers.strava.oauth import StravaOAuth
from app.services.providers.strava.workouts import StravaWorkouts
from app.utils.connection_context import active_connection
from tests.factories import UserConnectionFactory, UserFactory

START = "2026-10-06T02:43:45Z"


def _activity(activity_id: int, *, start: str = START, device_name: str | None = None) -> StravaActivityJSON:
    return StravaActivityJSON(
        id=activity_id,
        name="Run",
        type="Run",
        sport_type="Run",
        start_date=start,
        elapsed_time=600,
        utc_offset=-14_400.0,
        device_name=device_name,
    )


def _streams(bpm: int) -> dict[str, Any]:
    return {"time": {"data": list(range(600))}, "heartrate": {"data": [bpm] * 600}}


@pytest.fixture
def strava() -> StravaWorkouts:
    connection_repo = UserConnectionRepository()
    oauth = StravaOAuth(
        user_repo=MagicMock(),
        connection_repo=connection_repo,
        provider_name="strava",
        api_base_url="https://www.strava.com",
    )
    return StravaWorkouts(
        workout_repo=EventRecordRepository(EventRecord),
        connection_repo=connection_repo,
        provider_name="strava",
        api_base_url="https://www.strava.com",
        oauth=oauth,
    )


@pytest.fixture(autouse=True)
def _ingest_samples() -> Any:
    with patch.object(settings, "ingest_workout_samples", True):
        yield


def _push(
    strava: StravaWorkouts, db: Session, connection_id: Any, user_id: Any, activity: StravaActivityJSON, bpm: int
) -> None:
    with active_connection(connection_id), patch.object(strava, "_make_api_request", return_value=_streams(bpm)):
        strava.process_push_activity(db, activity, user_id)
    # Each webhook is its own task and transaction; a re-delivered activity's duplicate
    # record rolls the session back, which must not take the previous delivery with it.
    db.commit()


def _hr_by_source(db: Session, user_id: Any) -> dict[str, set[float]]:
    """Heart-rate values per data source, keyed "<device_model>|<source>"."""
    rows = db.execute(
        select(DataSource.device_model, DataSource.source, DataPointSeries.value)
        .join(DataSource, DataSource.id == DataPointSeries.data_source_id)
        .where(
            DataSource.user_id == user_id,
            DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.heart_rate),
        )
    ).all()
    out: dict[str, set[float]] = {}
    for device_model, source, value in rows:
        out.setdefault(f"{device_model}|{source}", set()).add(float(value))
    return out


def test_two_device_less_activities_at_the_same_time_keep_their_own_traces(strava: StravaWorkouts, db: Session) -> None:
    user = UserFactory()
    conn = UserConnectionFactory(user=user, provider="strava")

    _push(strava, db, conn.id, user.id, _activity(111), 140)
    _push(strava, db, conn.id, user.id, _activity(222), 150)

    by_source = _hr_by_source(db, user.id)
    assert len(by_source) == 2
    assert sorted(by_source.values(), key=min) == [{140.0}, {150.0}]
    assert any(source.endswith("(activity 222)") for source in by_source)


def test_activities_that_do_not_overlap_share_a_source_as_before(strava: StravaWorkouts, db: Session) -> None:
    user = UserFactory()
    conn = UserConnectionFactory(user=user, provider="strava")
    later = (datetime.fromisoformat(START.replace("Z", "+00:00")) + timedelta(hours=3)).isoformat()

    _push(strava, db, conn.id, user.id, _activity(111), 140)
    _push(strava, db, conn.id, user.id, _activity(333, start=later), 150)

    assert len(_hr_by_source(db, user.id)) == 1


def test_a_re_delivered_activity_lands_where_it_did_the_first_time(strava: StravaWorkouts, db: Session) -> None:
    user = UserFactory()
    conn = UserConnectionFactory(user=user, provider="strava")

    _push(strava, db, conn.id, user.id, _activity(111), 140)
    _push(strava, db, conn.id, user.id, _activity(222), 150)
    _push(strava, db, conn.id, user.id, _activity(222), 150)
    _push(strava, db, conn.id, user.id, _activity(111), 140)

    by_source = _hr_by_source(db, user.id)
    assert len(by_source) == 2
    assert sorted(by_source.values(), key=min) == [{140.0}, {150.0}]


def test_two_named_devices_were_already_apart_and_stay_on_their_own_names(strava: StravaWorkouts, db: Session) -> None:
    user = UserFactory()
    conn = UserConnectionFactory(user=user, provider="strava")

    _push(strava, db, conn.id, user.id, _activity(111, device_name="Garmin Forerunner 965"), 140)
    _push(strava, db, conn.id, user.id, _activity(222, device_name="Garmin Edge 1050"), 150)

    sources = set(_hr_by_source(db, user.id))
    assert len(sources) == 2
    assert not any("(activity" in (source or "") for source in sources)
