"""An account's dated device history: reading it, replacing it, and re-filing stored data by it.

Stating a timeline changes where *future* records land (the ingest paths resolve the
label by each record's own time - app/utils/device_timeline.py). Records already
stored stay where they were written until a re-file moves them, and a re-file is a
separate, explicit step with a dry run by default, because it rewrites attribution
on data that was already read and may already have been published from.

What a re-file will and will not move, each a deliberate refusal:

- **Only off a source whose device name a label put there**, or that has none. A
  model the provider stamped is a capture; a person's timeline never overrules it.
  Sources written before origin was recorded are reported and left alone unless the
  request names them.
- **Never over a row already at the destination.** Same source, same instant is the
  same reading; the pair is reported as a conflict and neither is touched.
- **Never an archived day the switch falls inside.** One daily aggregate cannot be
  split between two devices, so it stays and is counted.
- **Never a stretch no period covers.** Nothing was stated for it.
- **Only this account.** Every query is scoped by connection id, so a second account
  of the same provider - another participant's, or the same person's validation
  account wearing the same model - is never touched.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, delete, exists, func, or_, select, update
from sqlalchemy.orm import InstrumentedAttribute, aliased

from app.database import DbSession
from app.models import (
    DataPointSeries,
    DataPointSeriesArchive,
    DataSource,
    DeviceHistory,
    EventRecord,
    HealthScore,
    UserConnection,
    UserConnectionDevicePeriod,
)
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import DeviceHistoryAction, DeviceModelOrigin
from app.schemas.model_crud.user_management.device_timeline import (
    DevicePeriodRead,
    DeviceRefileRequest,
    DeviceRefileResult,
    DeviceTimelineRead,
    DeviceTimelineUpdate,
    RefileCounts,
    RefileMove,
    RefileSource,
)
from app.utils.device_timeline import DeviceSpan, DeviceTimeline, load_timeline

# Archive rows are daily aggregates (DataPointSeriesArchive).
ARCHIVE_BUCKET = timedelta(days=1)


class DeviceTimelineError(ValueError):
    """A request the timeline cannot satisfy; the route answers 400 with the message."""


def _in_span(column: InstrumentedAttribute[datetime], span: DeviceSpan) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if span.start is not None:
        conditions.append(column >= span.start)
    if span.end is not None:
        conditions.append(column < span.end)
    return conditions


@dataclass
class _Counts:
    event_records: int = 0
    samples: int = 0
    archive_days: int = 0
    health_scores: int = 0

    def any(self) -> bool:
        return bool(self.event_records or self.samples or self.archive_days or self.health_scores)

    def add(self, other: "_Counts") -> None:
        self.event_records += other.event_records
        self.samples += other.samples
        self.archive_days += other.archive_days
        self.health_scores += other.health_scores

    def read(self) -> RefileCounts:
        return RefileCounts(
            event_records=self.event_records,
            samples=self.samples,
            archive_days=self.archive_days,
            health_scores=self.health_scores,
        )


@dataclass
class _MoveResult:
    moved: _Counts = field(default_factory=_Counts)
    conflicts: _Counts = field(default_factory=_Counts)
    straddling: int = 0


class DeviceTimelineService:
    def __init__(self, log: Logger):
        self.log = log
        self.data_source_repo = DataSourceRepository()

    # ── reading and replacing ────────────────────────────────────────────────

    def get_timeline(self, db_session: DbSession, connection: UserConnection) -> DeviceTimelineRead:
        rows = (
            db_session.execute(
                select(UserConnectionDevicePeriod)
                .where(UserConnectionDevicePeriod.user_connection_id == connection.id)
                .order_by(UserConnectionDevicePeriod.effective_from.asc().nulls_first())
            )
            .scalars()
            .all()
        )
        periods = [DevicePeriodRead.model_validate(row) for row in rows]
        for current, following in zip(periods, periods[1:], strict=False):
            current.effective_to = following.effective_from
        return DeviceTimelineRead(
            connection_id=connection.id,
            provider=connection.provider,
            device_label=connection.device_label,
            periods=periods,
        )

    def replace_timeline(
        self,
        db_session: DbSession,
        connection: UserConnection,
        update_payload: DeviceTimelineUpdate,
    ) -> DeviceTimelineRead:
        """Replace the account's periods; the account's device_label follows the current one.

        An empty list removes the timeline and leaves device_label as it stands, so the
        account falls back to the undated label it now names - the last device stated.
        Nothing stored is moved here: that is refile(), asked for separately.
        """
        ordered = sorted(
            update_payload.periods,
            key=lambda p: (p.effective_from is not None, p.effective_from or datetime.min.replace(tzinfo=timezone.utc)),
        )
        db_session.execute(
            delete(UserConnectionDevicePeriod).where(UserConnectionDevicePeriod.user_connection_id == connection.id)
        )
        for period in ordered:
            db_session.add(
                UserConnectionDevicePeriod(
                    id=uuid4(),
                    user_connection_id=connection.id,
                    device_label=period.device_label,
                    effective_from=period.effective_from,
                )
            )
        if ordered:
            connection.device_label = ordered[-1].device_label
            connection.updated_at = datetime.now(timezone.utc)
            db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return self.get_timeline(db_session, connection)

    # ── re-filing what is already stored ─────────────────────────────────────

    def refile(
        self,
        db_session: DbSession,
        connection: UserConnection,
        request: DeviceRefileRequest,
        actor: str | None = None,
    ) -> DeviceRefileResult:
        timeline = load_timeline(db_session, connection.id)
        if timeline is None:
            raise DeviceTimelineError("This account has no dated device history to re-file by")

        sources = (
            db_session.execute(select(DataSource).where(DataSource.user_connection_id == connection.id)).scalars().all()
        )
        by_id = {s.id: s for s in sources}
        unknown = [str(i) for i in request.include_data_source_ids if i not in by_id]
        if unknown:
            raise DeviceTimelineError(f"Not data sources of this account: {', '.join(unknown)}")
        included = set(request.include_data_source_ids)

        verdicts = [self._verdict(s, included) for s in sources]
        eligible = [by_id[v.data_source_id] for v in verdicts if v.eligible]

        moves: list[RefileMove] = []
        total_moved, total_conflicts, uncovered = _Counts(), _Counts(), _Counts()

        for source in eligible:
            uncovered.add(self._uncovered(db_session, source, timeline))
            for span in timeline.spans():
                if span.device_label == source.device_model:
                    continue
                target = self._target(db_session, source, span.device_label, request.dry_run)
                result = self._move(db_session, source, target, span, request.dry_run)
                if not (result.moved.any() or result.conflicts.any() or result.straddling):
                    continue
                moves.append(
                    RefileMove(
                        from_data_source_id=source.id,
                        from_device_model=source.device_model,
                        to_device_model=span.device_label,
                        to_data_source_id=target.id if target is not None else None,
                        period_start=span.start,
                        period_end=span.end,
                        moved=result.moved.read(),
                        conflicts=result.conflicts.read(),
                        archive_days_straddling=result.straddling,
                    )
                )
                total_moved.add(result.moved)
                total_conflicts.add(result.conflicts)
                if not request.dry_run and target is not None:
                    db_session.add(
                        DeviceHistory(
                            id=uuid4(),
                            user_id=connection.user_id,
                            device_id=source.device_id,
                            data_source_id=source.id,
                            action=DeviceHistoryAction.REFILED.value,
                            old_value=source.device_model,
                            new_value=span.device_label,
                            actor=actor,
                            reason="dated device history",
                            meta={
                                "to_data_source_id": str(target.id),
                                "user_connection_id": str(connection.id),
                                "period_start": span.start.isoformat() if span.start else None,
                                "period_end": span.end.isoformat() if span.end else None,
                                "moved": result.moved.read().model_dump(),
                                "conflicts": result.conflicts.read().model_dump(),
                                "archive_days_straddling": result.straddling,
                            },
                        )
                    )

        if request.dry_run:
            db_session.rollback()
        else:
            db_session.commit()

        return DeviceRefileResult(
            connection_id=connection.id,
            dry_run=request.dry_run,
            sources=verdicts,
            moves=moves,
            uncovered=uncovered.read(),
            total_moved=total_moved.read(),
            total_conflicts=total_conflicts.read(),
        )

    @staticmethod
    def _verdict(source: DataSource, included: set[UUID]) -> RefileSource:
        def verdict(eligible: bool, reason: str) -> RefileSource:
            return RefileSource(
                data_source_id=source.id,
                device_model=source.device_model,
                device_model_origin=source.device_model_origin,
                source=source.source,
                eligible=eligible,
                reason=reason,
            )

        if source.device_model_origin == DeviceModelOrigin.PROVIDER.value:
            return verdict(False, "The provider named this device on the data; a timeline never overrules that")
        if source.device_model is None:
            return verdict(True, "No device named - the timeline supplies one")
        if source.device_model_origin == DeviceModelOrigin.LABEL.value:
            return verdict(True, "Device name came from this account's label")
        if source.id in included:
            return verdict(True, "Origin unrecorded; included by request")
        return verdict(
            False,
            "Origin unrecorded (written before OW tracked it). Include it only if the provider never "
            "reports a device on this account",
        )

    def _target(self, db_session: DbSession, source: DataSource, label: str, dry_run: bool) -> DataSource | None:
        """Where ``source``'s rows for a ``label`` period go; never created in a dry run."""
        if dry_run:
            return self.data_source_repo.find_label_source(db_session, source, label)
        return self.data_source_repo.ensure_label_source(db_session, source, label)

    def _uncovered(self, db_session: DbSession, source: DataSource, timeline: DeviceTimeline) -> _Counts:
        first = timeline.periods[0].effective_from if timeline.periods else None
        if first is None:
            return _Counts()
        return _Counts(
            event_records=self._count(
                db_session, EventRecord, EventRecord.start_datetime < first, EventRecord.data_source_id == source.id
            ),
            samples=self._count(
                db_session,
                DataPointSeries,
                DataPointSeries.recorded_at < first,
                DataPointSeries.data_source_id == source.id,
            ),
            archive_days=self._count(
                db_session,
                DataPointSeriesArchive,
                DataPointSeriesArchive.bucket_start_at < first,
                DataPointSeriesArchive.data_source_id == source.id,
            ),
        )

    @staticmethod
    def _count(db_session: DbSession, model: type, *conditions: ColumnElement[bool]) -> int:
        return int(db_session.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one())

    def _move(
        self,
        db_session: DbSession,
        source: DataSource,
        target: DataSource | None,
        span: DeviceSpan,
        dry_run: bool,
    ) -> _MoveResult:
        result = _MoveResult()
        target_id = target.id if target is not None else None

        # Event records: the unique key is (source, start, end).
        other_er = aliased(EventRecord)
        er_span = [EventRecord.data_source_id == source.id, *_in_span(EventRecord.start_datetime, span)]
        er_clash = exists().where(
            other_er.data_source_id == target_id,
            other_er.start_datetime == EventRecord.start_datetime,
            other_er.end_datetime == EventRecord.end_datetime,
        )
        er_total = self._count(db_session, EventRecord, *er_span)
        er_conflicts = self._count(db_session, EventRecord, *er_span, er_clash) if target_id else 0
        result.moved.event_records = er_total - er_conflicts
        result.conflicts.event_records = er_conflicts

        # Health scores follow their event record; free-standing ones go by their own time.
        hs_with_record = and_(
            HealthScore.data_source_id == source.id,
            HealthScore.event_record_id.in_(select(EventRecord.id).where(*er_span, ~er_clash)),
        )
        hs_alone = and_(
            HealthScore.data_source_id == source.id,
            HealthScore.event_record_id.is_(None),
            *_in_span(HealthScore.recorded_at, span),
        )
        result.moved.health_scores = self._count(db_session, HealthScore, hs_with_record) + self._count(
            db_session, HealthScore, hs_alone
        )

        # Samples: the unique key is (source, type, instant).
        other_dp = aliased(DataPointSeries)
        dp_span = [DataPointSeries.data_source_id == source.id, *_in_span(DataPointSeries.recorded_at, span)]
        dp_clash = exists().where(
            other_dp.data_source_id == target_id,
            other_dp.series_type_definition_id == DataPointSeries.series_type_definition_id,
            other_dp.recorded_at == DataPointSeries.recorded_at,
        )
        dp_total = self._count(db_session, DataPointSeries, *dp_span)
        dp_conflicts = self._count(db_session, DataPointSeries, *dp_span, dp_clash) if target_id else 0
        result.moved.samples = dp_total - dp_conflicts
        result.conflicts.samples = dp_conflicts

        # Archived days: whole days only. A day is inside the span when it starts at or
        # after the span's start and ends at or before the span's end.
        other_ar = aliased(DataPointSeriesArchive)
        ar_inside = [DataPointSeriesArchive.data_source_id == source.id]
        if span.start is not None:
            ar_inside.append(DataPointSeriesArchive.bucket_start_at >= span.start)
        if span.end is not None:
            ar_inside.append(DataPointSeriesArchive.bucket_start_at + ARCHIVE_BUCKET <= span.end)
        ar_clash = exists().where(
            other_ar.data_source_id == target_id,
            other_ar.series_type_definition_id == DataPointSeriesArchive.series_type_definition_id,
            other_ar.bucket_start_at == DataPointSeriesArchive.bucket_start_at,
            other_ar.aggregation_type == DataPointSeriesArchive.aggregation_type,
        )
        ar_total = self._count(db_session, DataPointSeriesArchive, *ar_inside)
        ar_conflicts = self._count(db_session, DataPointSeriesArchive, *ar_inside, ar_clash) if target_id else 0
        result.moved.archive_days = ar_total - ar_conflicts
        result.conflicts.archive_days = ar_conflicts
        # A boundary strictly inside a day: that day belongs to two periods at once.
        straddles = []
        for boundary in (span.start, span.end):
            if boundary is None:
                continue
            straddles.append(
                and_(
                    DataPointSeriesArchive.bucket_start_at < boundary,
                    DataPointSeriesArchive.bucket_start_at + ARCHIVE_BUCKET > boundary,
                )
            )
        if straddles:
            result.straddling = self._count(
                db_session,
                DataPointSeriesArchive,
                DataPointSeriesArchive.data_source_id == source.id,
                or_(*straddles),
            )

        if dry_run or target_id is None:
            return result

        # Scores first: their "record moved" test reads the records' current source.
        db_session.execute(
            update(HealthScore)
            .where(hs_with_record)
            .values(data_source_id=target_id)
            .execution_options(synchronize_session=False)
        )
        db_session.execute(
            update(HealthScore)
            .where(hs_alone)
            .values(data_source_id=target_id)
            .execution_options(synchronize_session=False)
        )
        db_session.execute(
            update(EventRecord)
            .where(*er_span, ~er_clash)
            .values(data_source_id=target_id)
            .execution_options(synchronize_session=False)
        )
        db_session.execute(
            update(DataPointSeries)
            .where(*dp_span, ~dp_clash)
            .values(data_source_id=target_id)
            .execution_options(synchronize_session=False)
        )
        db_session.execute(
            update(DataPointSeriesArchive)
            .where(*ar_inside, ~ar_clash)
            .values(data_source_id=target_id)
            .execution_options(synchronize_session=False)
        )
        db_session.flush()
        return result


device_timeline_service = DeviceTimelineService(log=getLogger(__name__))
