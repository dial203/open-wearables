"""CSV exports of workouts: the samples recorded during one, and a row per workout.

Two things decide what an exported file can be trusted for, and both are answered in
the file rather than left to whoever opens it:

- *Which device said this.* The sample export reads every stored sample in the
  workout's window, from every device that recorded one, and keeps each source apart.
  Nothing is narrowed to the workout's own device and nothing is pooled across devices:
  a Garmin, a WHOOP and an Apple Watch worn together come out as separate columns (wide)
  or separately attributed rows (long). Pooling them would produce a trace no device
  recorded, and it would look exactly like one that did.
- *What was derived.* The long layout is the stored samples untouched. The wide layout
  puts them on a 1-s grid, the only transformation either export applies. An empty cell
  is a second with no sample, never a zero, and a second that held more than one sample
  from a source is averaged and says so in a count column rather than silently.
"""

import csv
import io
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Row, func
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecord, User, WorkoutDetails
from app.schemas.enums import SeriesType, get_series_type_from_id, get_series_type_id, get_series_type_unit
from app.schemas.model_crud.activities import EventRecordQueryParams, WorkoutInclude
from app.schemas.responses.activity import Workout
from app.schemas.utils import SourceMetadata
from app.services.event_record_service import event_record_service
from app.services.sources.relay_dedup import RelayDedupPlan, build_series_plan
from app.utils.pagination import MAX_PAGE_SIZE

# Beat-to-beat intervals are events, one per heartbeat, not readings on a clock: several
# fall in most seconds, and a per-second mean of RR intervals is not a quantity anyone
# analyses. They stay out of the 1-s grid and are exported only in the long layout.
BEAT_INTERVAL_TYPES = frozenset({SeriesType.rr_interval, SeriesType.pulse_to_pulse_interval})

MAX_PAD_SECONDS = 3600

# Metrics per relay-dedup plan. The rule measures coverage per metric only while
# metrics x direct sources stays small, and falls back to per brand past that; four
# keeps it per metric for up to twelve direct sources with a relay to weigh.
_RELAY_PLAN_BATCH = 4

# Samples are stored to three decimals (numeric(10,3)); a mean of several gets the same.
_MEAN_PLACES = Decimal("0.001")

# A stored sample as the export reads it: recorded_at, zone_offset, series type id,
# value, data source id, provider_metadata.
_SampleRow = Row[tuple[datetime, str | None, int, Decimal, UUID, dict[str, Any] | None]]

# Heart rate first, since it is what most exports are for; everything else in id order.
_TYPE_ORDER_FIRST = (SeriesType.heart_rate,)


class SampleLayout(StrEnum):
    WIDE = "wide"
    LONG = "long"


@dataclass(frozen=True)
class CsvExport:
    filename: str
    content: str
    row_count: int  # samples read, or workouts listed
    source_count: int


@dataclass(frozen=True)
class _Column:
    data_source_id: UUID
    series_type: SeriesType
    label: str
    collides: bool


def _iso_second(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_millis(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _floor_second(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(microsecond=0)


def _number(value: Decimal | float | int | None) -> str:
    """A stored number as text, without float noise and without trailing zeros."""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        text = format(value.normalize(), "f")
    else:
        text = format(Decimal(str(value)).normalize(), "f")
    return "0" if text in ("-0", "") else text


def _text(value: object) -> str:
    """An optional enum or string as CSV text."""
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def _parse_offset(zone_offset: str | None) -> timedelta | None:
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", zone_offset or "")
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))


def _local(value: datetime, zone_offset: str | None) -> datetime | None:
    offset = _parse_offset(zone_offset)
    return None if offset is None else value.astimezone(UTC).replace(tzinfo=None) + offset


def _slug(value: str | None, fallback: str) -> str:
    """A filename part: letters, digits and underscores only.

    Hyphens are separators in the name this builds, which the HR validity tool splits
    on to read participant, activity and date back out.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
    return slug or fallback


def _header_safe(value: str) -> str:
    """A column label safe for naive CSV readers: no commas, quotes or line breaks."""
    return re.sub(r"\s+", " ", re.sub(r"[,\"\r\n]", " ", value)).strip()


def source_label(meta: SourceMetadata) -> str:
    """What to call one source in a column header.

    The registry's name for the physical unit where there is one, since that is what a
    person wearing three devices recognises; a relayed stream says which platform carried
    it, because the same watch read directly and through Apple Health are two sources.
    """
    base = (
        meta.device_display_name
        or meta.device_hardware_name
        or meta.device_name
        or meta.original_source_name
        or meta.source
        or meta.provider
    )
    if meta.ingestion_route == "aggregator" and meta.provider and meta.provider.lower() not in base.lower():
        base = f"{base} via {meta.provider}"
    return _header_safe(base)


def _metric_label(series_type: SeriesType) -> str:
    return f"{series_type.value} ({get_series_type_unit(series_type)})"


def _type_sort_key(series_type: SeriesType) -> tuple[int, int]:
    if series_type in _TYPE_ORDER_FIRST:
        return (0, _TYPE_ORDER_FIRST.index(series_type))
    return (1, get_series_type_id(series_type))


def _participant(user: User | None, user_id: UUID) -> str:
    return _slug(user.external_user_id if user else None, str(user_id)[:8])


def _to_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


class WorkoutExportService:
    """Builds the CSV files behind the workout export endpoints."""

    # ------------------------------------------------------------------
    # Samples recorded during one workout
    # ------------------------------------------------------------------

    def export_workout_samples(
        self,
        db_session: DbSession,
        user_id: UUID,
        workout_id: UUID,
        layout: SampleLayout = SampleLayout.WIDE,
        types: Sequence[SeriesType] = (),
        pad_seconds: int = 0,
        include_redundant_relays: bool = False,
    ) -> CsvExport | None:
        """Every sample stored during a workout, from every source, as CSV.

        None when the workout does not exist for this user. A workout with no samples in
        its window still exports: the file is the honest answer, and the counts say it.
        """
        workout = (
            db_session.query(EventRecord)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .filter(
                EventRecord.id == workout_id,
                EventRecord.category == "workout",
                DataSource.user_id == user_id,
            )
            .first()
        )
        if workout is None:
            return None

        pad = timedelta(seconds=max(0, min(pad_seconds, MAX_PAD_SECONDS)))
        start = workout.start_datetime - pad
        end = workout.end_datetime + pad

        # Asked for or not, beat intervals cannot sit on the grid (BEAT_INTERVAL_TYPES says
        # why); the long layout carries them. A wide request naming only those reads nothing
        # rather than falling through to "no filter", which would mean every type.
        exclude_types = BEAT_INTERVAL_TYPES if layout is SampleLayout.WIDE else frozenset()
        requested = list(dict.fromkeys(types))
        wanted = [t for t in requested if t not in exclude_types]
        nothing_to_read = bool(requested) and not wanted

        relay_plans = (
            []
            if nothing_to_read
            else self._relay_plans(
                db_session,
                user_id,
                wanted or self._types_present(db_session, user_id, start, end, exclude_types),
                start,
                end,
                include_redundant_relays,
            )
        )
        inventory = (
            []
            if nothing_to_read
            else self._inventory(db_session, user_id, start, end, wanted, exclude_types, relay_plans)
        )
        sources = self._load_sources(db_session, {row[0] for row in inventory})
        sample_count = sum(row[2] for row in inventory)

        local_start = _local(workout.start_datetime, workout.zone_offset)
        day = (local_start or workout.start_datetime.astimezone(UTC)).strftime("%Y%m%d")
        clock = (local_start or workout.start_datetime.astimezone(UTC)).strftime("%H%M")
        user = db_session.get(User, user_id)
        filename = f"{_participant(user, user_id)}-{_slug(workout.type, 'workout')}-{day}-{clock}-{layout.value}.csv"

        samples = (
            iter(())
            if nothing_to_read
            else self._samples(db_session, user_id, start, end, wanted, exclude_types, relay_plans)
        )
        if layout is SampleLayout.WIDE:
            columns = self._columns(inventory, sources, workout.data_source_id)
            content = _to_csv(
                ["timestamp_utc", *self._wide_header(columns)],
                self._wide_rows(samples, columns, start, end),
            )
        else:
            labels = self._labels(sources)
            content = _to_csv(
                self._LONG_HEADER,
                self._long_rows(samples, sources, labels, workout.start_datetime),
            )

        return CsvExport(
            filename=filename,
            content=content,
            row_count=sample_count,
            source_count=len(sources),
        )

    @staticmethod
    def _relay_plans(
        db_session: DbSession,
        user_id: UUID,
        types: Sequence[SeriesType],
        start: datetime,
        end: datetime,
        include_redundant_relays: bool,
    ) -> list[RelayDedupPlan]:
        """The same redundant-relay rule the timeseries read applies, decided per metric.

        A watch connected directly and also relayed through Apple Health is one device
        reached two ways; without the rule it would export as two devices agreeing
        suspiciously well.

        Per metric, always. Asked about no types, or about more than it measures one by
        one, the rule falls back to one span per brand and hides every metric of the
        relay the direct route was active over - a Garmin's running power from Apple
        Health, say, when Garmin itself sent only heart rate - and with it the only copy
        there is. So the types are always named, a few at a time; a span only ever
        filters its own type, so the plans' conditions combine without interfering.
        """
        if not settings.relay_dedup_enabled or include_redundant_relays or not types:
            return []
        type_ids = [get_series_type_id(t) for t in types]
        plans = [
            build_series_plan(db_session, user_id, type_ids[i : i + _RELAY_PLAN_BATCH], start, end)
            for i in range(0, len(type_ids), _RELAY_PLAN_BATCH)
        ]
        return [plan for plan in plans if plan.applied]

    def _types_present(
        self,
        db_session: DbSession,
        user_id: UUID,
        start: datetime,
        end: datetime,
        exclude_types: frozenset[SeriesType],
    ) -> list[SeriesType]:
        """The metrics any source recorded in the window, so the relay rule can be asked per metric."""
        rows = (
            db_session.query(DataPointSeries.series_type_definition_id)
            .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
            .filter(*self._sample_filters(user_id, start, end, (), exclude_types, ()))
            .distinct()
            .all()
        )
        return sorted((get_series_type_from_id(type_id) for (type_id,) in rows), key=_type_sort_key)

    @staticmethod
    def _sample_filters(
        user_id: UUID,
        start: datetime,
        end: datetime,
        types: Sequence[SeriesType],
        exclude_types: frozenset[SeriesType],
        relay_plans: Sequence[RelayDedupPlan],
    ) -> list[Any]:
        filters: list[Any] = [
            DataSource.user_id == user_id,
            DataPointSeries.recorded_at >= start,
            DataPointSeries.recorded_at <= end,
            # A daily total stamped inside the window is not something the workout
            # recorded.
            DataPointSeries.is_daily_total.isnot(True),
        ]
        if types:
            filters.append(DataPointSeries.series_type_definition_id.in_([get_series_type_id(t) for t in types]))
        if exclude_types:
            filters.append(
                DataPointSeries.series_type_definition_id.notin_([get_series_type_id(t) for t in exclude_types])
            )
        for plan in relay_plans:
            filters.extend(
                plan.conditions(
                    DataPointSeries.data_source_id,
                    key_column=DataPointSeries.series_type_definition_id,
                    timestamp_column=DataPointSeries.recorded_at,
                )
            )
        return filters

    def _inventory(
        self,
        db_session: DbSession,
        user_id: UUID,
        start: datetime,
        end: datetime,
        types: Sequence[SeriesType],
        exclude_types: frozenset[SeriesType],
        relay_plans: Sequence[RelayDedupPlan],
    ) -> list[tuple[UUID, int, int, bool]]:
        """(data source, series type, samples, any second holding several) per stream.

        Taken before the samples so the wide header is known up front and the samples
        can stream through one second at a time instead of being collected first.
        """
        second = func.date_trunc("second", DataPointSeries.recorded_at)
        rows = (
            db_session.query(
                DataPointSeries.data_source_id,
                DataPointSeries.series_type_definition_id,
                func.count(),
                func.count(func.distinct(second)),
            )
            .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
            .filter(*self._sample_filters(user_id, start, end, types, exclude_types, relay_plans))
            .group_by(DataPointSeries.data_source_id, DataPointSeries.series_type_definition_id)
            .all()
        )
        return [(source_id, type_id, count, count > seconds) for source_id, type_id, count, seconds in rows]

    def _samples(
        self,
        db_session: DbSession,
        user_id: UUID,
        start: datetime,
        end: datetime,
        types: Sequence[SeriesType],
        exclude_types: frozenset[SeriesType],
        relay_plans: Sequence[RelayDedupPlan],
    ) -> Iterator[_SampleRow]:
        query = (
            db_session.query(
                DataPointSeries.recorded_at,
                DataPointSeries.zone_offset,
                DataPointSeries.series_type_definition_id,
                DataPointSeries.value,
                DataPointSeries.data_source_id,
                DataPointSeries.provider_metadata,
            )
            .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
            .filter(*self._sample_filters(user_id, start, end, types, exclude_types, relay_plans))
            .order_by(
                DataPointSeries.recorded_at,
                DataPointSeries.data_source_id,
                DataPointSeries.series_type_definition_id,
            )
            .yield_per(5000)
        )
        yield from query

    @staticmethod
    def _load_sources(db_session: DbSession, source_ids: set[UUID]) -> dict[UUID, SourceMetadata]:
        if not source_ids:
            return {}
        rows = (
            db_session.query(DataSource)
            .options(selectinload(DataSource.device))
            .filter(DataSource.id.in_(source_ids))
            .all()
        )
        return {row.id: SourceMetadata.from_data_source(row) for row in rows}

    @staticmethod
    def _labels(sources: dict[UUID, SourceMetadata]) -> dict[UUID, str]:
        """One distinct label per source; two sources that name themselves alike get their id."""
        labels = {source_id: source_label(meta) for source_id, meta in sources.items()}
        counts: dict[str, int] = {}
        for label in labels.values():
            counts[label] = counts.get(label, 0) + 1
        return {
            source_id: label if counts[label] == 1 else f"{label} #{str(source_id)[:8]}"
            for source_id, label in labels.items()
        }

    def _columns(
        self,
        inventory: list[tuple[UUID, int, int, bool]],
        sources: dict[UUID, SourceMetadata],
        workout_source_id: UUID,
    ) -> list[_Column]:
        """Wide columns: the workout's own device first, then the others by name.

        First because the HR validity tool reads the first device column of an aligned
        file as its default criterion, so exporting from the chest strap's workout puts
        the strap there.
        """
        labels = self._labels(sources)
        streams = [
            (source_id, get_series_type_from_id(type_id), collides)
            for source_id, type_id, _count, collides in inventory
            if source_id in sources
        ]
        streams.sort(key=lambda s: (s[0] != workout_source_id, labels[s[0]].lower(), str(s[0]), _type_sort_key(s[1])))
        return [
            _Column(
                data_source_id=source_id,
                series_type=series_type,
                label=f"{labels[source_id]} | {_metric_label(series_type)}",
                collides=collides,
            )
            for source_id, series_type, collides in streams
        ]

    @staticmethod
    def _wide_header(columns: list[_Column]) -> list[str]:
        header: list[str] = []
        for column in columns:
            header.append(column.label)
            if column.collides:
                header.append(f"{column.label} n")
        return header

    @staticmethod
    def _wide_rows(
        samples: Iterator[_SampleRow],
        columns: list[_Column],
        start: datetime,
        end: datetime,
    ) -> Iterator[list[str]]:
        """One row per second of the window, every second, empty where nothing was recorded.

        Each sample goes to the second it was recorded in (its UTC timestamp truncated),
        so a dropout shows as a run of empty cells rather than as a shorter file.
        """
        index = {(c.data_source_id, get_series_type_id(c.series_type)): i for i, c in enumerate(columns)}
        width = len(columns)

        def render(second: datetime, cells: list[tuple[Decimal, int] | None]) -> list[str]:
            row = [_iso_second(second)]
            for column, cell in zip(columns, cells, strict=True):
                if cell is None:
                    row.append("")
                    if column.collides:
                        row.append("")
                    continue
                total, count = cell
                row.append(_number(total if count == 1 else (total / count).quantize(_MEAN_PLACES)))
                if column.collides:
                    row.append(str(count))
            return row

        current = _floor_second(start)
        last = _floor_second(end)
        cells: list[tuple[Decimal, int] | None] = [None] * width
        step = timedelta(seconds=1)
        for recorded_at, _zone, type_id, value, source_id, _meta in samples:
            position = index.get((source_id, type_id))
            if position is None:
                continue
            second = _floor_second(recorded_at)
            while current < second:
                yield render(current, cells)
                cells = [None] * width
                current += step
            total, count = cells[position] or (Decimal(0), 0)
            cells[position] = (total + Decimal(value), count + 1)
        while current <= last:
            yield render(current, cells)
            cells = [None] * width
            current += step

    _LONG_HEADER = (
        "timestamp_utc",
        "elapsed_s",
        "zone_offset",
        "series_label",
        "metric",
        "value",
        "unit",
        "interval_seconds",
        "device_display_name",
        "provider",
        "source",
        "original_source_name",
        "ingestion_route",
        "device_model",
        "device_type",
        "data_source_id",
        "device_id",
        "user_connection_id",
        "provider_metadata",
    )

    @staticmethod
    def _long_rows(
        samples: Iterator[_SampleRow],
        sources: dict[UUID, SourceMetadata],
        labels: dict[UUID, str],
        workout_start: datetime,
    ) -> Iterator[list[str]]:
        for recorded_at, zone_offset, type_id, value, source_id, provider_metadata in samples:
            meta = sources.get(source_id)
            if meta is None:
                continue
            series_type = get_series_type_from_id(type_id)
            interval = provider_metadata.get("interval_seconds") if isinstance(provider_metadata, dict) else None
            elapsed = (recorded_at - workout_start).total_seconds()
            yield [
                _iso_millis(recorded_at),
                f"{elapsed:.3f}",
                zone_offset or "",
                f"{labels[source_id]} | {_metric_label(series_type)}",
                series_type.value,
                _number(value),
                get_series_type_unit(series_type),
                str(interval) if isinstance(interval, int | float) and not isinstance(interval, bool) else "",
                meta.device_display_name or "",
                meta.provider or "",
                meta.source or "",
                meta.original_source_name or "",
                meta.ingestion_route or "",
                meta.device or "",
                _text(meta.device_type),
                str(source_id),
                str(meta.device_id or ""),
                str(meta.user_connection_id or ""),
                json.dumps(provider_metadata, separators=(",", ":"), sort_keys=True, default=str)
                if provider_metadata
                else "",
            ]

    # ------------------------------------------------------------------
    # One row per workout
    # ------------------------------------------------------------------

    _SUMMARY_HEAD = (
        "workout_id",
        "overlap_group",
        "type",
        "name",
        "start_time_utc",
        "end_time_utc",
        "zone_offset",
        "start_time_local",
        "duration_s",
        "moving_time_s",
        "distance_m",
        "calories_kcal",
        "avg_hr_bpm",
        "avg_hr_origin",
        "max_hr_bpm",
        "min_hr_bpm",
        "avg_pace_s_per_km",
        "average_speed_m_s",
        "max_speed_m_s",
        "average_cadence",
        "average_power_w",
        "max_power_w",
        "elevation_gain_m",
        "elev_high_m",
        "elev_low_m",
        "steps",
        "entry_source",
        "intensity",
    )
    _SUMMARY_TAIL = (
        "has_fit_file",
        "external_id",
        "series_label",
        "device_display_name",
        "provider",
        "source",
        "original_source_name",
        "ingestion_route",
        "device_model",
        "device_type",
        "data_source_id",
        "device_id",
        "user_connection_id",
    )

    def export_workout_summaries(
        self,
        db_session: DbSession,
        user_id: UUID,
        params: EventRecordQueryParams,
    ) -> CsvExport:
        """One row per workout the list endpoint would return for the same filters."""
        workouts = self._all_workouts(db_session, user_id, params)
        details = self._workout_detail_extras(db_session, [w.id for w in workouts])
        groups = overlap_groups([(w.id, w.start_time, w.end_time) for w in workouts])
        labels = self._labels(
            {w.source.data_source_id: w.source for w in workouts if w.source.data_source_id is not None}
        )

        hr_zone_ids = sorted({z.zone for w in workouts if w.hr_zones for z in w.hr_zones.zones})
        power_zone_ids = sorted({z.zone for w in workouts if w.power_zones for z in w.power_zones.zones})
        header = [
            *self._SUMMARY_HEAD,
            *(f"hr_zone_{z}_s" for z in hr_zone_ids),
            *(f"power_zone_{z}_s" for z in power_zone_ids),
            *self._SUMMARY_TAIL,
        ]

        def row(workout: Workout) -> list[str]:
            external_id, provider_avg_hr = details.get(workout.id, (None, None))
            if provider_avg_hr is not None:
                avg_hr, avg_hr_origin = _number(provider_avg_hr), "provider"
            elif workout.avg_heart_rate_bpm is not None:
                # Worked out by OW from this workout's own heart-rate samples, because the
                # provider did not report one. Marked so it is not read as the device's figure.
                avg_hr, avg_hr_origin = _number(workout.avg_heart_rate_bpm), "computed_from_samples"
            else:
                avg_hr, avg_hr_origin = "", ""
            local_start = _local(workout.start_time, workout.zone_offset)
            hr_zones = {z.zone: z.seconds for z in workout.hr_zones.zones} if workout.hr_zones else {}
            power_zones = {z.zone: z.seconds for z in workout.power_zones.zones} if workout.power_zones else {}
            source = workout.source
            return [
                str(workout.id),
                str(groups[workout.id]),
                workout.type,
                workout.name or "",
                _iso_second(workout.start_time),
                _iso_second(workout.end_time),
                workout.zone_offset or "",
                local_start.strftime("%Y-%m-%d %H:%M:%S") if local_start else "",
                _number(workout.duration_seconds),
                _number(workout.moving_time_seconds),
                _number(workout.distance_meters),
                _number(workout.calories_kcal),
                avg_hr,
                avg_hr_origin,
                _number(workout.max_heart_rate_bpm),
                _number(workout.heart_rate_min),
                _number(workout.avg_pace_sec_per_km),
                _number(workout.average_speed),
                _number(workout.max_speed),
                _number(workout.average_cadence),
                _number(workout.average_watts),
                _number(workout.max_watts),
                _number(workout.elevation_gain_meters),
                _number(workout.elev_high),
                _number(workout.elev_low),
                _number(workout.steps_count),
                _text(workout.entry_source),
                _text(workout.intensity),
                *(_number(hr_zones.get(z)) for z in hr_zone_ids),
                *(_number(power_zones.get(z)) for z in power_zone_ids),
                "true" if workout.has_fit_file else "false",
                external_id or "",
                labels.get(source.data_source_id, "") if source.data_source_id else "",
                source.device_display_name or "",
                source.provider or "",
                source.source or "",
                source.original_source_name or "",
                source.ingestion_route or "",
                source.device or "",
                _text(source.device_type),
                str(source.data_source_id or ""),
                str(source.device_id or ""),
                str(source.user_connection_id or ""),
            ]

        user = db_session.get(User, user_id)
        first = _day(params.start_datetime) or (workouts[0].start_time.strftime("%Y%m%d") if workouts else "all")
        last = _day(params.end_datetime) or (workouts[-1].start_time.strftime("%Y%m%d") if workouts else "all")
        return CsvExport(
            filename=f"{_participant(user, user_id)}-workouts-{first}-{last}.csv",
            content=_to_csv(header, (row(w) for w in workouts)),
            row_count=len(workouts),
            source_count=len(labels),
        )

    @staticmethod
    def _all_workouts(db_session: DbSession, user_id: UUID, params: EventRecordQueryParams) -> list[Workout]:
        """Every page of the workout list, so the export is exactly what the list holds."""
        workouts: list[Workout] = []
        cursor: str | None = None
        while True:
            page = event_record_service.get_workouts(
                db_session,
                user_id,
                params.model_copy(update={"cursor": cursor, "limit": MAX_PAGE_SIZE}),
                include=[WorkoutInclude.ZONES],
            )
            workouts.extend(page.data)
            cursor = page.pagination.next_cursor
            if not page.pagination.has_more or not cursor:
                break
        workouts.sort(key=lambda w: (w.start_time, str(w.id)))
        return workouts

    @staticmethod
    def _workout_detail_extras(
        db_session: DbSession, workout_ids: list[UUID]
    ) -> dict[UUID, tuple[str | None, Decimal | None]]:
        """The provider's own id and unrounded average HR, which the list response drops."""
        if not workout_ids:
            return {}
        rows = (
            db_session.query(EventRecord.id, EventRecord.external_id, WorkoutDetails.heart_rate_avg)
            .outerjoin(WorkoutDetails, WorkoutDetails.record_id == EventRecord.id)
            .filter(EventRecord.id.in_(workout_ids))
            .all()
        )
        return {record_id: (external_id, hr_avg) for record_id, external_id, hr_avg in rows}


def _day(value: datetime | date | None) -> str | None:
    return value.strftime("%Y%m%d") if value else None


def overlap_groups(spans: Sequence[tuple[UUID, datetime, datetime]]) -> dict[UUID, int]:
    """Number workouts so that ones overlapping in time share a number.

    Two devices worn for one session each file their own workout, at slightly different
    start and end times; this is what lets the summary export pair them back up. Overlap
    is transitive (A overlaps B, B overlaps C: one group), and spans that merely touch
    end-to-start are separate sessions.
    """
    groups: dict[UUID, int] = {}
    group = 0
    group_end: datetime | None = None
    for workout_id, start, end in sorted(spans, key=lambda s: (s[1], s[2], str(s[0]))):
        if group_end is None or start >= group_end:
            group += 1
            group_end = end
        else:
            group_end = max(group_end, end)
        groups[workout_id] = group
    return groups


workout_export_service = WorkoutExportService()
