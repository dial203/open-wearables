from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Any, Iterable
from uuid import UUID, uuid4

import isodate

from app.constants.series_types.polar import RR_INTERVAL_SAMPLE_TYPE
from app.constants.workout_types.polar import get_unified_workout_type
from app.database import DbSession
from app.schemas.enums import ProviderName, SeriesType
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
    TimeSeriesSampleCreate,
)
from app.schemas.providers.polar import ExerciseJSON as PolarExerciseJSON
from app.services.event_record_service import event_record_service
from app.services.polar_rr_import_service import polar_rr_import_service
from app.services.providers.polar.v4_data import polar_v4_data
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.services.timeseries_service import timeseries_service
from app.utils.dates import offset_to_iso


class PolarWorkouts(BaseWorkoutsTemplate):
    """Polar implementation of workouts template."""

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[Any]:
        """Get exercises from Polar API."""
        return self._make_api_request(db, user_id, "/v3/exercises")

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        """Get exercises from Polar API with options.

        ``samples`` defaults to True so the sync path receives the per-exercise sample
        arrays, which is where AccessLink carries beat-to-beat RR intervals (sample type
        11, recorded only with an H6/H7/H9/H10 chest strap).
        """
        samples = kwargs.get("samples", True)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)

        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, "/v3/exercises", params=params)

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        """Get detailed exercise data from Polar API."""
        samples = kwargs.get("samples", False)
        zones = kwargs.get("zones", False)
        route = kwargs.get("route", False)
        return self.get_exercise_detail(db, user_id, workout_id, samples, zones, route)

    def _extract_dates(self, start_timestamp: Any, end_timestamp: Any) -> tuple[datetime, datetime]:
        """Extract start and end dates from timestamps.

        Note: Polar uses a different format with offset, so this delegates to _extract_dates_with_offset.
        This is required by the base template but not used directly.
        """
        raise NotImplementedError("Use _extract_dates_with_offset for Polar workouts")

    @staticmethod
    def _local_start(start_time: str) -> datetime:
        """The exercise start as Polar reports it: naive wall-clock time where it was recorded."""
        return isodate.parse_datetime(start_time).replace(tzinfo=None)

    def _extract_dates_with_offset(
        self,
        start_time: str,
        start_time_utc_offset: int,
        duration: str,
    ) -> tuple[datetime, datetime]:
        """Convert Polar's local start time and UTC offset into a UTC start and end.

        ``start_time`` is naive local time and ``start_time_utc_offset`` is that zone's offset
        from UTC in minutes, so UTC is local *minus* the offset. Adding it instead, as this
        once did, stored every exercise twice the offset away from when it happened: 8 h
        early in US Eastern daylight time, so an overnight chest-strap session sat on the
        previous afternoon and no read over the sleep window could find its RR.
        """
        local_start = self._local_start(start_time)
        start_date = (local_start - timedelta(minutes=start_time_utc_offset)).replace(tzinfo=timezone.utc)
        end_date = start_date + isodate.parse_duration(duration)
        return start_date, end_date

    def _build_metrics(self, raw_workout: PolarExerciseJSON) -> EventRecordMetrics:
        hr_avg = (
            Decimal(str(raw_workout.heart_rate.average))
            if raw_workout.heart_rate and raw_workout.heart_rate.average is not None
            else None
        )
        hr_max = (
            Decimal(str(raw_workout.heart_rate.maximum))
            if raw_workout.heart_rate and raw_workout.heart_rate.maximum is not None
            else None
        )

        energy_burned = Decimal(str(raw_workout.calories)) if raw_workout.calories is not None else None

        distance = Decimal(str(raw_workout.distance)) if raw_workout.distance is not None else None

        return {
            "heart_rate_max": int(hr_max) if hr_max is not None else None,
            "heart_rate_avg": hr_avg,
            "energy_burned": energy_burned,
            "distance": distance,
        }

    def _normalize_workout(
        self,
        raw_workout: PolarExerciseJSON,
        user_id: UUID,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Normalize Polar exercise to EventRecordCreate and EventRecordDetailCreate."""
        workout_id = uuid4()

        workout_type = get_unified_workout_type(raw_workout.sport, raw_workout.detailed_sport_info)

        start_date, end_date = self._extract_dates_with_offset(
            raw_workout.start_time,
            raw_workout.start_time_utc_offset,
            raw_workout.duration,
        )
        duration_seconds = int((end_date - start_date).total_seconds())

        metrics = self._build_metrics(raw_workout)

        # convert from offset minutes to seconds first
        zone_offset = offset_to_iso(raw_workout.start_time_utc_offset * 60)

        record = EventRecordCreate(
            category="workout",
            type=workout_type.value,
            source_name=raw_workout.device,
            device_model=raw_workout.device,
            duration_seconds=duration_seconds,
            start_datetime=start_date,
            end_datetime=end_date,
            zone_offset=zone_offset,
            id=workout_id,
            external_id=raw_workout.id,
            source="polar",
            user_id=user_id,
        )

        detail = EventRecordDetailCreate(
            record_id=workout_id,
            **metrics,
        )

        return record, detail

    # -------------------------------------------------------------------------
    # RR intervals — AccessLink exercise sample type 11
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_rr_sample_data(data: str) -> list[int | None]:
        """Split an RR sample payload into per-beat intervals in ms.

        AccessLink ships samples as one comma-separated string. RR is the only sample
        type that may carry NULL entries (missing beats) — they arrive as an empty field
        or the literal ``null`` and are kept as ``None`` so the beat count stays honest.
        Non-positive or unparseable values are treated as missing too.
        """
        values: list[int | None] = []
        for raw_value in data.split(","):
            token = raw_value.strip()
            if not token or token.lower() == "null":
                values.append(None)
                continue
            try:
                interval = int(float(token))
            except ValueError:
                values.append(None)
                continue
            values.append(interval if interval > 0 else None)
        return values

    def _build_rr_samples(
        self,
        raw_workout: PolarExerciseJSON,
        user_id: UUID,
        start_date: datetime,
        zone_offset: str | None,
        duration_seconds: int,
    ) -> list[TimeSeriesSampleCreate]:
        """Reconstruct per-beat RR samples for one exercise, if it carries any.

        The API gives no per-beat clock, so each beat is timestamped at the R-wave that
        *closes* its interval: start time plus the cumulative sum of the intervals before
        it. Missing (NULL) beats have no duration of their own, so the clock would drift
        early across a dropout. Instead the unaccounted time — the exercise duration minus
        the sum of the valid intervals — is spread evenly over the missing beats, which
        keeps the series anchored at both ends of the session; where the deficit is
        non-positive (e.g. a paused session) the median valid interval is used instead.
        Only real beats are stored; the estimate is a clock, never a value.
        """
        sample = next(
            (s for s in (raw_workout.samples or []) if s.sample_type == RR_INTERVAL_SAMPLE_TYPE),
            None,
        )
        if sample is None or not sample.data:
            return []

        values = self._parse_rr_sample_data(sample.data)
        valid = [v for v in values if v is not None]
        if not valid:
            return []

        missing = len(values) - len(valid)
        gap_ms = 0.0
        if missing:
            deficit = duration_seconds * 1000 - sum(valid)
            gap_ms = deficit / missing if deficit > 0 else float(median(valid))
            self.logger.info(
                "Polar exercise %s: %d of %d RR beats missing; gap estimated at %.1f ms",
                raw_workout.id,
                missing,
                len(values),
                gap_ms,
            )

        samples: list[TimeSeriesSampleCreate] = []
        elapsed_ms = 0.0
        for interval_ms in values:
            elapsed_ms += interval_ms if interval_ms is not None else gap_ms
            if interval_ms is None:
                continue
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider=ProviderName.POLAR,
                    source=ProviderName.POLAR,
                    device_model=raw_workout.device,
                    external_id=raw_workout.id,
                    recorded_at=start_date + timedelta(milliseconds=elapsed_ms),
                    zone_offset=zone_offset,
                    value=interval_ms,
                    series_type=SeriesType.rr_interval,
                )
            )
        return samples

    def _rr_samples_for(
        self,
        db: DbSession,
        raw_workout: PolarExerciseJSON,
        user_id: UUID,
        day_cache: dict[date, list[Any]] | None = None,
    ) -> list[TimeSeriesSampleCreate]:
        """RR samples for an exercise, preferring v4's exact beat clock over v3's estimate.

        v4 gives every beat its own duration, offline ones included, so nothing has to be
        estimated across a dropout. We ask it first and only reconstruct from v3's sample
        type 11 when v4 has nothing for this session — no v4 token, no matching session, or
        a session recorded without a strap. Exactly one of the two is stored, because two
        reconstructions of the same beats land on near-identical timestamps and would leave
        a doubled series that no HRV analysis can untangle.
        """
        start_date, end_date = self._extract_dates_with_offset(
            raw_workout.start_time,
            raw_workout.start_time_utc_offset,
            raw_workout.duration,
        )
        zone_offset = offset_to_iso(raw_workout.start_time_utc_offset * 60)

        try:
            # v4 reports sessions in local wall-clock time, so match on the local start.
            v4_rows = polar_v4_data.rr_rows_for_session(
                db, user_id, self._local_start(raw_workout.start_time), day_cache
            )
        except Exception as exc:  # noqa: BLE001 - v4 is an upgrade, never a dependency
            self.logger.warning(
                "Polar v4 RR lookup failed for exercise %s, falling back to v3: %s", raw_workout.id, exc
            )
            v4_rows = None

        if v4_rows:
            self.logger.info("Polar exercise %s: using v4 RR (%d beats, exact clock)", raw_workout.id, len(v4_rows))
            return polar_rr_import_service.build_creators(
                v4_rows,
                user_id=user_id,
                start_datetime=start_date,
                source=ProviderName.POLAR.value,
                device_model=raw_workout.device,
                zone_offset=zone_offset,
            )

        return self._build_rr_samples(
            raw_workout,
            user_id,
            start_date,
            zone_offset,
            int((end_date - start_date).total_seconds()),
        )

    def _save_rr_samples(self, db: DbSession, samples: list[TimeSeriesSampleCreate]) -> int:
        """Persist reconstructed RR beats. Re-syncs upsert on (source, type, recorded_at)."""
        if not samples:
            return 0
        counts = timeseries_service.bulk_create_samples(db, samples)
        return int(counts)

    def _build_bundles(
        self,
        raw: list[PolarExerciseJSON],
        user_id: UUID,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate]]:
        """Build event record payloads for Polar exercises."""
        for raw_workout in raw:
            yield self._normalize_workout(raw_workout, user_id)

    def load_data(
        self,
        db: DbSession,
        user_id: UUID,
        **kwargs: Any,
    ) -> int:
        """Load data from Polar API."""
        workouts_data = self.get_workouts_from_api(db, user_id, **kwargs)
        workouts = [PolarExerciseJSON(**w) for w in workouts_data]

        count = 0
        # One cache for the whole pull: Flow lists every exercise of the last 30 days on
        # each sync, and they cluster onto few days.
        day_cache: dict[date, list[Any]] = {}
        for raw_workout, (record, detail) in zip(workouts, self._build_bundles(workouts, user_id), strict=True):
            created_record = event_record_service.create(db, record)
            detail_for_record = detail.model_copy(update={"record_id": created_record.id})
            event_record_service.create_detail(db, detail_for_record)
            # Flushed per exercise: a night on a chest strap is ~30k beats, so holding
            # every session's beats until the end of a backfill is a lot of memory.
            self._save_rr_samples(db, self._rr_samples_for(db, raw_workout, user_id, day_cache))
            count += 1

        return count

    def fetch_and_save_exercise(self, db: DbSession, user_id: UUID, path: str) -> int:
        """Fetch a single exercise by URL path and save it. Used by webhook handler.

        Asks for samples so a chest-strap session's RR intervals land with the workout
        instead of waiting for the next pull.
        """
        raw = self._make_api_request(db, user_id, path, params={"samples": "true"})
        if not raw:
            return 0
        exercise = PolarExerciseJSON(**raw)
        count = 0
        for record, detail in self._build_bundles([exercise], user_id):
            created = event_record_service.create(db, record)
            event_record_service.create_detail(db, detail.model_copy(update={"record_id": created.id}))
            count += 1
        self._save_rr_samples(db, self._rr_samples_for(db, exercise, user_id))
        return count

    def get_exercise_detail(
        self,
        db: DbSession,
        user_id: UUID,
        exercise_id: str,
        samples: bool = False,
        zones: bool = False,
        route: bool = False,
    ) -> dict:
        """Get detailed exercise data from Polar API."""
        params = {
            "samples": str(samples).lower(),
            "zones": str(zones).lower(),
            "route": str(route).lower(),
        }
        return self._make_api_request(db, user_id, f"/v3/exercises/{exercise_id}", params=params)
