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

One relaxation of the first rule, for the providers that name a device on workouts
and on nothing else (app/utils/device_switch_detection.EVIDENCE_PROVIDERS): on those a
capture can be told apart row by row, so a source the provider named - or one of
unrecorded origin - gives up only what the provider could not have named. Its workouts
stay, with every sample inside one; its nights, daily totals, out-of-workout samples
and their scores move. Archived days stay whole, since one daily row can mix both, and
are reported. That matters here because a label spelled exactly as Garmin spells the
watch shares its data source with that watch's activities: without it the nights on
that source could never move, whether a person or detection dated them.

Detection itself (detect()) replaces only detected periods after the account's
detect_from and then re-files, so a new watch moves the records it should without
anyone opening the dashboard. Saving a history by hand (replace_timeline) sets
detect_from to that moment: what the person saved stays as saved, and detection only
adds switches it sees after it.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging import Logger, getLogger
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, and_, delete, exists, false, func, or_, select, update
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
    WorkoutDetails,
)
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import DeviceHistoryAction, DeviceModelOrigin, DevicePeriodOrigin, EntrySource
from app.schemas.model_crud.user_management.device_timeline import (
    DetectedPeriodRead,
    DeviceDetectionRead,
    DeviceDetectRequest,
    DeviceDetectResult,
    DevicePeriodRead,
    DeviceRefileRequest,
    DeviceRefileResult,
    DeviceSightingRead,
    DeviceTimelineRead,
    DeviceTimelineUpdate,
    RefileCounts,
    RefileMove,
    RefileSource,
)
from app.utils.device_switch_detection import (
    DetectedPeriod,
    Evidence,
    detect_periods,
    device_key,
    is_evidence_device,
    provider_detects_switches,
    sightings,
)
from app.utils.device_timeline import DevicePeriod, DeviceSpan, DeviceTimeline, load_timeline

# Archive rows are daily aggregates (DataPointSeriesArchive).
ARCHIVE_BUCKET = timedelta(days=1)
# The one event category the detection providers name a device on.
WORKOUT = "workout"


class DeviceTimelineError(ValueError):
    """A request the timeline cannot satisfy; the route answers 400 with the message."""


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def _same_instant(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return a is b
    return _utc(a) == _utc(b)


def _previous_last_seen(evidence: list[Evidence], label: str, start: datetime | None) -> datetime | None:
    """The last workout before ``start`` on a device other than ``label``."""
    if start is None:
        return None
    key = device_key(label)
    for item in reversed(evidence):
        if _utc(item.at) < _utc(start) and device_key(item.device_model) != key:
            return _utc(item.at)
    return None


@dataclass(frozen=True)
class _PeriodRow:
    device_label: str
    effective_from: datetime | None
    origin: str


def _as_period(row: _PeriodRow) -> DevicePeriod:
    return DevicePeriod(row.device_label, row.effective_from)


def _from_detected(period: DetectedPeriod) -> _PeriodRow:
    return _PeriodRow(period.device_label, period.effective_from, DevicePeriodOrigin.DETECTED.value)


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
    archive_kept: int = 0


class DeviceTimelineService:
    def __init__(self, log: Logger):
        self.log = log
        self.data_source_repo = DataSourceRepository()

    # ── reading and replacing ────────────────────────────────────────────────

    def _rows(self, db_session: DbSession, connection: UserConnection) -> list[UserConnectionDevicePeriod]:
        return list(
            db_session.execute(
                select(UserConnectionDevicePeriod)
                .where(UserConnectionDevicePeriod.user_connection_id == connection.id)
                .order_by(UserConnectionDevicePeriod.effective_from.asc().nulls_first())
            )
            .scalars()
            .all()
        )

    def get_timeline(self, db_session: DbSession, connection: UserConnection) -> DeviceTimelineRead:
        rows = self._rows(db_session, connection)
        evidence = self.evidence(db_session, connection)
        periods = [DevicePeriodRead.model_validate(row) for row in rows]
        for current, following in zip(periods, periods[1:], strict=False):
            current.effective_to = following.effective_from
        for period in periods:
            period.previous_last_seen = _previous_last_seen(evidence, period.device_label, period.effective_from)
        return DeviceTimelineRead(
            connection_id=connection.id,
            provider=connection.provider,
            device_label=connection.device_label,
            periods=periods,
            auto=connection.device_timeline_auto,
            detect_from=connection.device_timeline_detect_from,
            detection=self._detection(connection, evidence, rows),
        )

    @staticmethod
    def _detection(
        connection: UserConnection,
        evidence: list[Evidence],
        rows: list[UserConnectionDevicePeriod],
    ) -> DeviceDetectionRead:
        if not provider_detects_switches(connection.provider):
            return DeviceDetectionRead(supported=False, proposed=[], devices=[], differs=False)
        proposed = detect_periods(evidence)
        saved = [(row.device_label, row.effective_from) for row in rows]
        differs = bool(proposed) and (
            len(saved) != len(proposed)
            or any(
                label != p.device_label or not _same_instant(start, p.effective_from)
                for (label, start), p in zip(saved, proposed, strict=True)
            )
        )
        return DeviceDetectionRead(
            supported=True,
            proposed=[DetectedPeriodRead(**vars(p)) for p in proposed],
            devices=[DeviceSightingRead(**vars(d)) for d in sightings(evidence)],
            differs=differs,
        )

    def evidence(self, db_session: DbSession, connection: UserConnection) -> list[Evidence]:
        """The account's workouts its provider named a body-worn device on, oldest first.

        A label never counts (it is what detection is meant to replace), nor does a
        workout someone typed in. A source of unrecorded origin does count: on the
        providers detection reads, a label was only ever put on a workout the provider
        sent without a device, and those are the typed-in ones already excluded.
        """
        if not provider_detects_switches(connection.provider):
            return []
        rows = db_session.execute(
            select(EventRecord.start_datetime, DataSource.device_model)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .outerjoin(WorkoutDetails, WorkoutDetails.record_id == EventRecord.id)
            .where(
                DataSource.user_connection_id == connection.id,
                DataSource.device_model.is_not(None),
                DataSource.device_model_origin.is_distinct_from(DeviceModelOrigin.LABEL.value),
                EventRecord.category == WORKOUT,
                WorkoutDetails.entry_source.is_distinct_from(EntrySource.MANUAL.value),
            )
            .order_by(EventRecord.start_datetime.asc())
        ).all()
        counts: dict[str, bool] = {}
        evidence: list[Evidence] = []
        for at, model in rows:
            if model not in counts:
                counts[model] = is_evidence_device(model)
            if counts[model]:
                evidence.append(Evidence(at=_utc(at), device_model=model))
        return evidence

    def _write_periods(self, db_session: DbSession, connection: UserConnection, periods: list[_PeriodRow]) -> None:
        """Replace the account's periods; device_label follows the current one. Not committed."""
        db_session.execute(
            delete(UserConnectionDevicePeriod).where(UserConnectionDevicePeriod.user_connection_id == connection.id)
        )
        for period in periods:
            db_session.add(
                UserConnectionDevicePeriod(
                    id=uuid4(),
                    user_connection_id=connection.id,
                    device_label=period.device_label,
                    effective_from=period.effective_from,
                    origin=period.origin,
                )
            )
        if periods:
            connection.device_label = periods[-1].device_label
        connection.updated_at = datetime.now(timezone.utc)
        db_session.add(connection)

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

        A period saved exactly as detection had it (same device, same start) keeps its
        "detected" origin; any other is "stated". When the periods change at all,
        detection is held to switches after this moment (or after the last start, if
        that is later), so a period someone removed or re-dated is not put back.
        Saving the same periods again - to change ``auto`` only - leaves that alone.
        """
        existing = self._rows(db_session, connection)
        detected = {
            (row.device_label, _utc(row.effective_from) if row.effective_from else None)
            for row in existing
            if row.origin == DevicePeriodOrigin.DETECTED.value
        }
        ordered = sorted(
            update_payload.periods,
            key=lambda p: (p.effective_from is not None, p.effective_from or datetime.min.replace(tzinfo=timezone.utc)),
        )
        incoming = [
            _PeriodRow(
                device_label=p.device_label,
                effective_from=p.effective_from,
                origin=(
                    DevicePeriodOrigin.DETECTED.value
                    if (p.device_label, _utc(p.effective_from) if p.effective_from else None) in detected
                    else DevicePeriodOrigin.STATED.value
                ),
            )
            for p in ordered
        ]
        unchanged = len(existing) == len(incoming) and all(
            row.device_label == period.device_label and _same_instant(row.effective_from, period.effective_from)
            for row, period in zip(existing, incoming, strict=True)
        )
        if not unchanged:
            now = datetime.now(timezone.utc)
            starts = [_utc(p.effective_from) for p in incoming if p.effective_from is not None]
            connection.device_timeline_detect_from = max([now, *starts])
            self._write_periods(db_session, connection, incoming)
        if update_payload.auto is not None:
            connection.device_timeline_auto = update_payload.auto
            db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)
        return self.get_timeline(db_session, connection)

    # ── detecting from the provider's own device names ───────────────────────

    def detect(
        self,
        db_session: DbSession,
        connection: UserConnection,
        request: DeviceDetectRequest | None = None,
        actor: str | None = None,
    ) -> DeviceDetectResult:
        """Bring the history in line with the account's workouts, then re-file if it changed.

        Without ``reset``, periods a person stated and anything before detect_from stay
        as they are; detected periods after it are rebuilt from the workouts after it,
        continuing from the device the history names at that instant. With ``reset`` the
        whole history is rebuilt and detect_from cleared.

        The re-file moves only what a re-file may (see the module docstring), and is
        recorded per move in device_history like any other.
        """
        request = request or DeviceDetectRequest()
        if not provider_detects_switches(connection.provider):
            raise DeviceTimelineError(
                f"{connection.provider} does not name devices on workouts; there is nothing to detect from"
            )

        existing = self._rows(db_session, connection)
        evidence = self.evidence(db_session, connection)
        detect_from = None if request.reset else connection.device_timeline_detect_from

        if detect_from is None:
            kept: list[_PeriodRow] = []
            found = detect_periods(evidence)
        else:
            kept = [
                _PeriodRow(row.device_label, row.effective_from, row.origin)
                for row in existing
                if row.origin == DevicePeriodOrigin.STATED.value
                or row.effective_from is None
                or _utc(row.effective_from) <= _utc(detect_from)
            ]
            # A history someone emptied stood on the undated label, from the start.
            base = kept or (
                [_PeriodRow(connection.device_label, None, DevicePeriodOrigin.STATED.value)]
                if connection.device_label
                else []
            )
            current = DeviceTimeline(tuple(_as_period(p) for p in base)).label_at(detect_from) if base else None
            found = detect_periods(evidence, after=detect_from, current_label=current)
            if found:
                kept = base
        merged = kept + [_from_detected(p) for p in found]

        changed = len(merged) != len(existing) or any(
            row.device_label != period.device_label
            or not _same_instant(row.effective_from, period.effective_from)
            or row.origin != period.origin
            for row, period in zip(existing, merged, strict=False)
        )
        if request.reset and connection.device_timeline_detect_from is not None:
            connection.device_timeline_detect_from = None
            db_session.add(connection)
            changed = True

        if not changed:
            return DeviceDetectResult(changed=False, timeline=self.get_timeline(db_session, connection))

        self._write_periods(db_session, connection, merged)
        db_session.commit()
        db_session.refresh(connection)
        self.log.info(
            "device history for connection %s rebuilt from %d workouts: %d periods (%s)",
            connection.id,
            len(evidence),
            len(merged),
            "reset" if request.reset else "incremental",
        )

        refile_result = None
        if request.refile and merged:
            refile_result = self.refile(
                db_session,
                connection,
                DeviceRefileRequest(dry_run=False),
                actor=actor or "device-switch-detection",
            )
        return DeviceDetectResult(
            changed=True,
            timeline=self.get_timeline(db_session, connection),
            refile=refile_result,
        )

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

        names_workouts_only = provider_detects_switches(connection.provider)
        verdicts = [self._verdict(s, included, names_workouts_only) for s in sources]

        moves: list[RefileMove] = []
        total_moved, total_conflicts, uncovered = _Counts(), _Counts(), _Counts()

        for verdict in verdicts:
            if not verdict.eligible:
                continue
            source = by_id[verdict.data_source_id]
            keep_captures = verdict.captures_kept
            uncovered.add(self._uncovered(db_session, source, timeline, keep_captures))
            for span in timeline.spans():
                if source.device_model is not None and device_key(span.device_label) == device_key(source.device_model):
                    continue
                target = self._target(db_session, source, span.device_label, request.dry_run)
                result = self._move(db_session, source, target, span, request.dry_run, keep_captures)
                if not (result.moved.any() or result.conflicts.any() or result.straddling or result.archive_kept):
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
                        archive_days_kept=result.archive_kept,
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
                                "captures_kept": keep_captures,
                                "moved": result.moved.read().model_dump(),
                                "conflicts": result.conflicts.read().model_dump(),
                                "archive_days_straddling": result.straddling,
                                "archive_days_kept": result.archive_kept,
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
    def _verdict(source: DataSource, included: set[UUID], names_workouts_only: bool) -> RefileSource:
        def verdict(eligible: bool, reason: str, captures_kept: bool = False) -> RefileSource:
            return RefileSource(
                data_source_id=source.id,
                device_model=source.device_model,
                device_model_origin=source.device_model_origin,
                source=source.source,
                eligible=eligible,
                captures_kept=captures_kept,
                reason=reason,
            )

        if source.device_model is None:
            return verdict(True, "No device named - the timeline supplies one")
        if source.device_model_origin == DeviceModelOrigin.LABEL.value:
            return verdict(True, "Device name came from this account's label")
        if source.device_model_origin is None and source.id in included:
            return verdict(True, "Origin unrecorded; included by request")
        if names_workouts_only:
            # Provider-named or unrecorded, on a provider that names a device on
            # workouts and nothing else: its workouts are captures, the rest is not.
            return verdict(
                True,
                "Its workouts (and samples inside them) are the provider's own and stay; "
                "nights, daily totals and other samples on it move",
                captures_kept=True,
            )
        if source.device_model_origin == DeviceModelOrigin.PROVIDER.value:
            return verdict(False, "The provider named this device on the data; a timeline never overrules that")
        return verdict(
            False,
            "Origin unrecorded (written before OW tracked it). Include it only if its device name is a "
            "label someone typed, not one the provider sent",
        )

    def _target(self, db_session: DbSession, source: DataSource, label: str, dry_run: bool) -> DataSource | None:
        """Where ``source``'s rows for a ``label`` period go; never created in a dry run."""
        if dry_run:
            return self.data_source_repo.find_label_source(db_session, source, label)
        return self.data_source_repo.ensure_label_source(db_session, source, label)

    @staticmethod
    def _event_conditions(source: DataSource, span: DeviceSpan, keep_captures: bool) -> list[ColumnElement[bool]]:
        conditions = [EventRecord.data_source_id == source.id, *_in_span(EventRecord.start_datetime, span)]
        if keep_captures:
            conditions.append(EventRecord.category != WORKOUT)
        return conditions

    @staticmethod
    def _sample_conditions(source: DataSource, span: DeviceSpan, keep_captures: bool) -> list[ColumnElement[bool]]:
        conditions = [DataPointSeries.data_source_id == source.id, *_in_span(DataPointSeries.recorded_at, span)]
        if keep_captures:
            # A sample inside one of this source's own workouts was recorded by it.
            workout = aliased(EventRecord)
            conditions.append(
                ~exists().where(
                    workout.data_source_id == source.id,
                    workout.category == WORKOUT,
                    workout.start_datetime <= DataPointSeries.recorded_at,
                    workout.end_datetime >= DataPointSeries.recorded_at,
                )
            )
        return conditions

    def _uncovered(
        self, db_session: DbSession, source: DataSource, timeline: DeviceTimeline, keep_captures: bool
    ) -> _Counts:
        first = timeline.periods[0].effective_from if timeline.periods else None
        if first is None:
            return _Counts()
        before = DeviceSpan("", None, first)
        return _Counts(
            event_records=self._count(db_session, EventRecord, *self._event_conditions(source, before, keep_captures)),
            samples=self._count(db_session, DataPointSeries, *self._sample_conditions(source, before, keep_captures)),
            archive_days=0
            if keep_captures
            else self._count(
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
        keep_captures: bool = False,
    ) -> _MoveResult:
        result = _MoveResult()
        target_id = target.id if target is not None else None

        # Event records: the unique key is (source, start, end).
        other_er = aliased(EventRecord)
        er_span = self._event_conditions(source, span, keep_captures)
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
        dp_span = self._sample_conditions(source, span, keep_captures)
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
        if keep_captures:
            # One daily row can hold the device's own workout samples beside the
            # nights', and cannot be split: it stays, and is counted.
            result.archive_kept = self._count(db_session, DataPointSeriesArchive, *ar_inside)
            ar_inside.append(false())
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
        if not keep_captures:
            db_session.execute(
                update(DataPointSeriesArchive)
                .where(*ar_inside, ~ar_clash)
                .values(data_source_id=target_id)
                .execution_options(synchronize_session=False)
            )
        db_session.flush()
        return result


device_timeline_service = DeviceTimelineService(log=getLogger(__name__))
