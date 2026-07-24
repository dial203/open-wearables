"""Import raw RR-interval (beat-to-beat) CSVs exported from Polar Flow.

Polar Flow's per-exercise "RR" CSV is a header line ``duration,offline`` followed
by one row per heartbeat: ``duration`` is the RR interval in milliseconds and
``offline`` marks beats recorded while the strap lost contact (dropouts — not real
intervals). The file carries no timestamps, so the per-beat clock is reconstructed
by cumulative-summing the durations from the workout's start time.

Ingested as an ``rr_interval`` time series tied to the workout's data source, so
downstream apps pull gold-standard RR from OW like any other series.
"""

from datetime import datetime, timedelta
from logging import Logger, getLogger
from uuid import UUID, uuid4

from app.database import DbSession
from app.models import DataPointSeries
from app.repositories import DataPointSeriesRepository
from app.schemas.enums import SeriesType
from app.schemas.model_crud.activities import TimeSeriesSampleCreate
from app.services.event_record_service import event_record_service

# A parsed RR row: (interval_ms, offline).
RrRow = tuple[int, bool]


class PolarRrImportError(ValueError):
    """Raised when the RR CSV cannot be parsed."""


class PolarRrImportService:
    def __init__(self, log: Logger) -> None:
        self.logger = log
        self.data_point_series_repo = DataPointSeriesRepository(DataPointSeries)

    @staticmethod
    def parse_rr_csv(contents: bytes) -> list[RrRow]:
        """Parse a Polar Flow RR CSV into (interval_ms, offline) rows.

        Tolerates a missing/renamed header (any non-numeric first line is skipped).
        Rows with a non-positive interval are dropped.
        """
        text = contents.decode("utf-8-sig", errors="replace")
        rows: list[RrRow] = []
        for lineno, raw_line in enumerate(text.splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            try:
                interval = int(parts[0])
            except (ValueError, IndexError):
                if lineno == 0:  # header row, e.g. "duration,offline"
                    continue
                raise PolarRrImportError(f"Malformed RR row at line {lineno + 1}: {raw_line!r}")
            offline = len(parts) > 1 and parts[1].strip().lower() in ("true", "1", "yes")
            if interval > 0:
                rows.append((interval, offline))
        if not rows:
            raise PolarRrImportError("RR CSV contained no interval rows")
        return rows

    @staticmethod
    def build_creators(
        rows: list[RrRow],
        *,
        user_id: UUID,
        start_datetime: datetime,
        source: str | None,
        device_model: str | None,
        zone_offset: str | None,
    ) -> list[TimeSeriesSampleCreate]:
        """Turn parsed RR rows into data-point creators tied to the workout's source.

        The clock advances over *every* interval (including offline dropouts) so the
        timeline stays correct, but only valid (online) beats are stored. Each beat is
        timestamped at the R-wave that *closes* the interval (start + cumulative sum).
        """
        creators: list[TimeSeriesSampleCreate] = []
        elapsed_ms = 0
        for interval_ms, offline in rows:
            elapsed_ms += interval_ms
            if offline:
                continue  # dropout gap: keep the clock moving, don't store a bogus beat
            creators.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    user_id=user_id,
                    provider="polar",
                    source=source,
                    device_model=device_model,
                    recorded_at=start_datetime + timedelta(milliseconds=elapsed_ms),
                    zone_offset=zone_offset,
                    value=interval_ms,
                    series_type=SeriesType.rr_interval,
                )
            )
        return creators

    @staticmethod
    def _zone_offset_from(start_datetime: datetime) -> str | None:
        """Format a tz-aware datetime's UTC offset as ``+HH:MM`` (None if naive)."""
        offset = start_datetime.utcoffset()
        if offset is None:
            return None
        total = int(offset.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"

    def _ingest(
        self,
        db_session: DbSession,
        *,
        user_id: UUID,
        start_datetime: datetime,
        source: str | None,
        device_model: str | None,
        zone_offset: str | None,
        contents: bytes,
        label: str,
    ) -> dict[str, int | str]:
        """Parse, timestamp, and persist an RR CSV. Shared by both import paths."""
        rows = self.parse_rr_csv(contents)
        creators = self.build_creators(
            rows,
            user_id=user_id,
            start_datetime=start_datetime,
            source=source,
            device_model=device_model,
            zone_offset=zone_offset,
        )
        written = self.data_point_series_repo.bulk_create(db_session, creators)
        db_session.commit()

        offline_skipped = len(rows) - len(creators)
        self.logger.info(
            "Imported Polar RR (%s): %d beats stored (%d offline skipped)",
            label,
            len(creators),
            offline_skipped,
        )
        return {
            "beats_stored": int(written),
            "beats_offline_skipped": offline_skipped,
            "beats_total": len(rows),
        }

    def import_rr_csv(
        self,
        db_session: DbSession,
        user_id: UUID,
        workout_id: UUID,
        contents: bytes,
    ) -> dict[str, int | str] | None:
        """Import an RR CSV for a known workout. Returns a summary, or None if the workout
        doesn't exist for this user (caller maps None to 404).

        Raises PolarRrImportError on a malformed CSV (caller maps to 400).
        """
        record = event_record_service.crud.get_record_with_details(db_session, workout_id, "workout")
        if not record:
            return None
        data_source = event_record_service.data_source_repo.get(db_session, record.data_source_id)
        if not data_source or data_source.user_id != user_id:
            return None

        result = self._ingest(
            db_session,
            user_id=user_id,
            start_datetime=record.start_datetime,
            source=data_source.source,
            device_model=data_source.device_model,
            zone_offset=record.zone_offset,
            contents=contents,
            label=f"workout {workout_id}",
        )
        return {"workout_id": str(workout_id), **result}

    def import_rr_csv_by_start_time(
        self,
        db_session: DbSession,
        user_id: UUID,
        start_datetime: datetime,
        contents: bytes,
        device: str | None = None,
    ) -> dict[str, int | str]:
        """Import an RR CSV attributed by session start time, without a pre-matched workout.

        Used when the source (e.g. Polar Flow) exposes only its own session id, not OW's
        workout id. The beats are attributed to the user's Polar data source (matching an
        existing Polar workout's source when ``device`` matches); the RR series is then
        queryable via ``/timeseries?types=rr_interval`` over the session window.

        Raises PolarRrImportError on a malformed CSV (caller maps to 400).
        """
        return self._ingest(
            db_session,
            user_id=user_id,
            start_datetime=start_datetime,
            source="polar",
            device_model=device,
            zone_offset=self._zone_offset_from(start_datetime),
            contents=contents,
            label=f"start_time {start_datetime.isoformat()}",
        )


polar_rr_import_service = PolarRrImportService(log=getLogger(__name__))
