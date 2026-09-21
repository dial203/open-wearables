"""Polar AccessLink v4 reads: exact RR intervals and optical pulse intervals.

v4 is used alongside v3 rather than instead of it. v3 keeps the webhook push that v4 has
no equivalent for, and v4 supplies the two things v3 cannot:

* **RR with an exact beat clock.** v3 ships RR as a bare comma-separated list whose
  missing beats are untimed NULLs, so the timeline has to be estimated across a dropout.
  Every v4 beat carries its own ``durationMillis``, offline ones included, so the clock is
  exact — which is the whole point when the series is the ECG criterion in a validation study.
* **24/7 pulse-to-pulse intervals**, with per-beat quality flags.

Both are stored against the user's ordinary Polar source, so a v4 read is invisible to
anything downstream except that the data is better. Everything here degrades to a no-op
when the user has not connected a v4 token.
"""

from datetime import date, datetime, timedelta
from logging import getLogger
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status

from app.database import DbSession
from app.models import User
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.schemas.providers.polar.v4 import (
    ListPpiSamplesResponseJSON,
    ListTrainingSessionsResponseJSON,
    TrainingSessionJSON,
)
from app.services.providers.api_client import make_authenticated_request
from app.services.providers.polar.v4_oauth import POLAR_V4_BASE_URL, PolarV4OAuth
from app.services.timeseries_service import timeseries_service

# How far apart a v3 exercise and a v4 training session may start and still be the same
# session. The two APIs report the same recording from different stores, so a few seconds
# of skew is normal; minutes would mean a different session.
SESSION_MATCH_TOLERANCE = timedelta(seconds=120)


class PolarV4Data:
    """Reads the v4 endpoints with the user's v4 token, if they hold one."""

    def __init__(self) -> None:
        self.logger = getLogger(__name__)
        self.connection_repo = UserConnectionRepository()
        self.oauth = PolarV4OAuth(
            user_repo=UserRepository(User),
            connection_repo=self.connection_repo,
            provider_name=ProviderName.POLAR_V4.value,
            api_base_url=POLAR_V4_BASE_URL,
        )

    # -------------------------------------------------------------------------
    # Transport
    # -------------------------------------------------------------------------

    def is_connected(self, db: DbSession, user_id: UUID) -> bool:
        """True when the user has authorised v4. Everything else here checks this first."""
        return self.connection_repo.get_active_connection(db, user_id, ProviderName.POLAR_V4.value) is not None

    def _get(self, db: DbSession, user_id: UUID, endpoint: str, params: dict[str, Any]) -> Any:
        """GET a v4 endpoint, treating "nothing there" as None rather than an error.

        A v4 read is never the only source of anything, so a failure degrades to the v3
        path instead of failing the sync.
        """
        try:
            return make_authenticated_request(
                db=db,
                user_id=user_id,
                connection_repo=self.connection_repo,
                oauth=self.oauth,
                api_base_url=POLAR_V4_BASE_URL,
                provider_name=ProviderName.POLAR_V4.value,
                endpoint=endpoint,
                method="GET",
                params=params,
            )
        except HTTPException as exc:
            if exc.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
                self.logger.info("Polar v4 not authorised for user %s (%s)", user_id, endpoint)
                return None
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                return None
            if exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR and "Expecting value" in str(exc.detail):
                return None  # 204 No Content on an empty day
            raise

    # -------------------------------------------------------------------------
    # Training sessions — exact RR
    # -------------------------------------------------------------------------

    def get_training_sessions(self, db: DbSession, user_id: UUID, day: date) -> list[TrainingSessionJSON]:
        """Training sessions for one local day, with samples.

        v4 caps the range at a single day whenever a feature is requested, so this is
        per-day by construction rather than by choice.
        """
        raw = self._get(
            db,
            user_id,
            "/v4/data/training-sessions/list",
            {"from": day.isoformat(), "to": (day + timedelta(days=1)).isoformat(), "features": "samples"},
        )
        if not raw:
            return []
        parsed = ListTrainingSessionsResponseJSON(**raw)
        return parsed.training_sessions or []

    @staticmethod
    def _session_start(session: TrainingSessionJSON) -> datetime | None:
        """A v4 session's start in its own local clock, matching how v3 reports exercises."""
        if not session.start_time:
            return None
        try:
            start = datetime.fromisoformat(session.start_time)
        except ValueError:
            return None
        return start.replace(tzinfo=None)

    def rr_rows_for_session(
        self,
        db: DbSession,
        user_id: UUID,
        start_datetime: datetime,
        day_cache: dict[date, list[TrainingSessionJSON]] | None = None,
    ) -> list[tuple[int, bool]] | None:
        """The (interval_ms, offline) beats v4 holds for the session starting at this time.

        Returns None when v4 has nothing to say — no connection, no matching session, or a
        session recorded without a strap — which is the caller's signal to fall back to v3.
        The rows are deliberately in the same shape as Polar Flow's RR CSV so both import
        paths share one beat clock.

        ``day_cache`` is a caller-owned dict for one unit of work. A pull re-walks every
        exercise Flow still lists, which without it would re-request the same v4 day once
        per exercise. It is never held across calls, so it cannot go stale.
        """
        if not self.is_connected(db, user_id):
            return None

        cache = day_cache if day_cache is not None else {}

        def sessions_on(day: date) -> list[TrainingSessionJSON]:
            if day not in cache:
                cache[day] = self.get_training_sessions(db, user_id, day)
            return cache[day]

        naive_start = start_datetime.replace(tzinfo=None)
        sessions = sessions_on(naive_start.date())
        # A session that began just before local midnight is filed under the previous day.
        if not sessions:
            sessions = sessions_on(naive_start.date() - timedelta(days=1))

        for session in sessions:
            session_start = self._session_start(session)
            if session_start is None or abs(session_start - naive_start) > SESSION_MATCH_TOLERANCE:
                continue
            rows = [
                (beat.duration_millis, beat.offline)
                for exercise in (session.exercises or [])
                for beat in ((exercise.samples.rr_samples if exercise.samples else None) or [])
                if beat.duration_millis > 0
            ]
            if rows:
                return rows
        return None

    # -------------------------------------------------------------------------
    # Pulse-to-pulse intervals — the optical comparator
    # -------------------------------------------------------------------------

    def normalize_ppi(self, raw: dict[str, Any], user_id: UUID) -> list[TimeSeriesSampleCreate]:
        """Turn a /ppi-samples response into samples timestamped from the start of each day.

        Beats recorded off the wrist or during an offline period are not measurements, so
        they are dropped. ``movement`` beats are kept: motion artefact is part of what an
        optical comparator does, and removing it here would flatter the device.
        """
        parsed = ListPpiSamplesResponseJSON(**raw)
        samples: list[TimeSeriesSampleCreate] = []
        dropped = 0

        for day in parsed.daily_ppi_samples or []:
            if not day.date:
                continue
            try:
                midnight = datetime.fromisoformat(day.date)
            except ValueError:
                continue
            for device in day.ppi_samples_per_device or []:
                for beat in device.ppi_samples or []:
                    if beat.offline or not beat.skin_contact or beat.pp_interval <= 0:
                        dropped += 1
                        continue
                    samples.append(
                        TimeSeriesSampleCreate(
                            id=uuid4(),
                            user_id=user_id,
                            provider=ProviderName.POLAR,
                            source=ProviderName.POLAR,
                            recorded_at=midnight + timedelta(milliseconds=beat.offset_millis),
                            value=beat.pp_interval,
                            series_type=SeriesType.pulse_to_pulse_interval,
                        )
                    )
        if dropped:
            self.logger.info("Polar v4 PPI: dropped %d beats (offline or no skin contact)", dropped)
        return samples

    def load_ppi_samples(self, db: DbSession, user_id: UUID, start_time: datetime, end_time: datetime) -> int:
        """Pull and store PPI for a date range. Returns the number of beats written."""
        if not self.is_connected(db, user_id):
            return 0

        written = 0
        day = start_time.date()
        last = end_time.date()
        while day <= last:
            raw = self._get(
                db,
                user_id,
                "/v4/data/ppi-samples",
                {"from": day.isoformat(), "to": (day + timedelta(days=1)).isoformat(), "features": "samples"},
            )
            if raw:
                samples = self.normalize_ppi(raw, user_id)
                if samples:
                    written += int(timeseries_service.bulk_create_samples(db, samples))
            day += timedelta(days=1)
        return written


polar_v4_data = PolarV4Data()
