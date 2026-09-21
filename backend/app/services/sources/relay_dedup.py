"""Hide the aggregator's copy of a maker's data when the maker is connected directly.

Someone who wears several brands ends up connecting each maker's API *and* an
aggregator - Apple Health, Google Health, Health Connect - because a few devices
have no other route. The aggregator then re-exposes everything the phone already
holds, so an Oura ring read from Oura's API arrives a second time as "Oura" inside
Apple Health, and every list, chart and export shows the ring twice.

What this module decides is narrow: **for the span the direct route actually
covers, the aggregator's copy of that same brand is redundant and is left out of
reads.** Everything else stays visible.

Three properties this rule is built to keep, in order of how badly their absence
would hurt:

1. **Nothing is dropped at ingest.** Relayed rows keep being written exactly as
   before; this only filters reads, and `include_redundant_relays=true` brings them
   straight back. A direct integration that silently stops (expired token, provider
   outage, a backfill that never reached last winter) leaves the relay as the only
   copy of those days, and a rule that had deleted it would have destroyed data no
   one could recover. It is also what keeps "the same ring, direct vs via Apple
   Health" a comparison this platform can still run.

2. **Suppression is bounded by what the direct route actually delivered.** Not by
   "a Garmin connection exists" - by the span in which direct Garmin samples of the
   *same series type* are really there, computed per request from the rows
   themselves, plus a few minutes' tolerance at each edge for the re-timestamping an
   aggregator does (SAMPLE_EDGE_TOLERANCE / EVENT_EDGE_TOLERANCE below). Outside that
   span the relay is the only evidence and stays visible, so connecting Garmin today
   does not blank out the Apple-relayed Garmin history from before it was connected,
   and a direct feed that stops delivering hands visibility back to the relay without
   anyone touching a setting.

3. **It never hides a second unit.** Two Garmins where only one is connected
   directly is the case that makes a naive brand-level rule lose data. A relayed
   source already attributed to a physical device that no direct source shares is
   therefore left alone - that device is reaching us only through the aggregator.

Known limits, stated because they decide whether an analysis can trust this:

- Coverage is a single [first, last] span per brand and series type within the
  requested window, not a per-day map. A gap *inside* that span - the direct API
  missing a Tuesday it should have had - keeps the relay hidden for the Tuesday
  too. Reads with `include_redundant_relays=true` to check completeness.
- The unit of suppression is a brand, not a device: with two Garmins both connected
  directly, a relayed Garmin stream that names no device is hidden across the span
  even if it came from the one that syncs less. `relay_visibility='always'` on that
  source is the escape hatch.
- The edge tolerance cuts both ways: relayed rows within a few minutes of the direct
  route's first or last row are treated as covered, so genuinely new relayed data in
  that window is hidden until the direct route has been quiet for longer than the
  tolerance.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from app.database import DbSession
from app.models import DataPointSeries, DataPointSeriesArchive, DataSource, EventRecord, HealthScore
from app.schemas.enums import IngestionRoute, ProviderName, RelayVisibility
from app.schemas.utils.metadata import HiddenRelayInfo, RelayDedupMetadata
from app.utils.device_registry import (
    AGGREGATOR_PROVIDERS,
    PROVIDER_BRANDS,
    direct_provider_for_brand,
    resolve_ingestion_route,
)

# Why a source was left out of a read, reported back in the response metadata so a
# missing stream is always traceable to a decision rather than looking like a gap.
REASON_COVERED = "covered_by_direct"
REASON_MANUAL = "hidden_by_hand"


@dataclass(frozen=True)
class HiddenRelay:
    """One relayed source left out of a read, and what made it redundant."""

    data_source_id: UUID
    # The aggregator that relayed it (apple, google_health, ...).
    provider: str
    # The maker whose data it carries (Oura, Garmin, ...).
    brand: str
    # The provider that delivered the same brand directly, and so made this copy
    # redundant. None when a person hid the source by hand.
    direct_provider: str | None
    covered_from: datetime | None
    covered_until: datetime | None
    reason: str


@dataclass(frozen=True)
class _Span:
    """Rows of ``source_ids`` to leave out: those matching ``key`` inside [start, end].

    ``key`` is a series type id for sample reads and an event category for event
    reads; ``None`` means every key. ``start``/``end`` of ``None`` mean unbounded,
    which only happens for a source hidden by hand.
    """

    source_ids: tuple[UUID, ...]
    key: int | str | None
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class RelayDedupPlan:
    """What a single read should leave out, resolved against that read's window."""

    spans: tuple[_Span, ...] = ()
    hidden: tuple[HiddenRelay, ...] = ()

    @property
    def applied(self) -> bool:
        return bool(self.spans)

    @property
    def hidden_source_ids(self) -> frozenset[UUID]:
        """Every source this plan hides somewhere in the requested window.

        Source-level, so it suits callers that pick one source per series type and
        would otherwise crown a relay whose rows are then filtered away, returning
        an empty series instead of the direct one.
        """
        return frozenset(source_id for span in self.spans for source_id in span.source_ids)

    def conditions(
        self,
        data_source_id_column: "InstrumentedAttribute[UUID] | InstrumentedAttribute[UUID | None]",
        *,
        key_column: InstrumentedAttribute | None = None,
        timestamp_column: InstrumentedAttribute[datetime] | None = None,
    ) -> list[ColumnElement[bool]]:
        """SQL saying "not one of the redundant rows", or nothing when there are none.

        One negated OR rather than a NOT IN per span: a row is kept unless it matches
        a span whole - source *and* key *and* timestamp - so a relay is filtered only
        where the direct route really covers it.

        A row whose data source is NULL is kept explicitly. ``NULL IN (...)`` is NULL,
        and ``NOT NULL`` is NULL, which SQL drops from a WHERE - so without the guard
        this would silently delete every health score written before scores carried a
        data source, which is the opposite of what the rule is for.
        """
        matches: list[ColumnElement[bool]] = []
        for span in self.spans:
            parts: list[ColumnElement[bool]] = [data_source_id_column.in_(span.source_ids)]
            if span.key is not None and key_column is not None:
                parts.append(key_column == span.key)
            if timestamp_column is not None:
                if span.start is not None:
                    parts.append(timestamp_column >= span.start)
                if span.end is not None:
                    parts.append(timestamp_column <= span.end)
            matches.append(and_(*parts))
        if not matches:
            return []
        return [or_(data_source_id_column.is_(None), ~or_(*matches))]


def _provider_slug(provider: object) -> str:
    """The provider as a plain slug, whether the row carries the enum or a string."""
    return str(getattr(provider, "value", provider) or "")


def _visibility(data_source: DataSource) -> RelayVisibility:
    raw = getattr(data_source, "relay_visibility", None) or RelayVisibility.AUTO
    try:
        return RelayVisibility(raw)
    except ValueError:
        # An unknown value is somebody's future setting, not a licence to hide data.
        return RelayVisibility.AUTO


def _brand_key(data_source: DataSource) -> str | None:
    """The maker a source is filed under, casefolded for comparison."""
    slug = _provider_slug(data_source.provider)
    brand = data_source.original_source_name
    if not brand:
        try:
            brand = PROVIDER_BRANDS.get(ProviderName(slug))
        except ValueError:
            brand = None
    return brand.strip().casefold() if brand else None


@dataclass(frozen=True)
class _Candidates:
    """The relayed sources worth testing, and the direct sources that would cover them."""

    # brand key -> relayed sources carrying that brand through an aggregator
    relays: dict[str, list[DataSource]]
    # brand key -> sources of the same brand that arrived by the maker's own route
    directs: dict[str, list[DataSource]]
    # sources a person hid by hand, which need no coverage evidence at all
    manual: list[DataSource]


def _candidates(db_session: DbSession, user_id: UUID) -> _Candidates:
    sources = db_session.query(DataSource).filter(DataSource.user_id == user_id).all()

    directs: dict[str, list[DataSource]] = {}
    for source in sources:
        slug = _provider_slug(source.provider)
        try:
            provider = ProviderName(slug)
        except ValueError:
            continue
        if provider in AGGREGATOR_PROVIDERS:
            continue
        brand = _brand_key(source)
        if brand:
            directs.setdefault(brand, []).append(source)

    relays: dict[str, list[DataSource]] = {}
    manual: list[DataSource] = []
    for source in sources:
        visibility = _visibility(source)
        if visibility is RelayVisibility.NEVER:
            manual.append(source)
            continue
        if visibility is RelayVisibility.ALWAYS:
            continue
        if resolve_ingestion_route(source.provider, source.original_source_name) is not IngestionRoute.AGGREGATOR:
            continue
        brand = _brand_key(source)
        if not brand or brand not in directs:
            continue
        if direct_provider_for_brand(brand) is None:
            # A brand with no API of its own (a third-party HealthKit writer) can only
            # ever reach us relayed, so nothing it carries is a second copy.
            continue
        # A relayed source already attributed to a device that no direct source shares
        # is a unit reaching us only this way - a second Garmin whose account is not
        # connected. Hiding it would lose that device entirely.
        direct_devices = {d.device_id for d in directs[brand] if d.device_id is not None}
        if source.device_id is not None and source.device_id not in direct_devices:
            continue
        relays.setdefault(brand, []).append(source)

    return _Candidates(relays=relays, directs=directs, manual=manual)


# How far past the first and last direct row a relayed copy still counts as covered.
#
# An aggregator re-timestamps what it relays: the same heartbeat arrives half a minute
# later, the same night's sleep starts four minutes off. Measured strictly, the copy of
# the *newest* direct row therefore always falls outside the covered span - and the
# newest row is exactly the one a person is looking at. A small tolerance at each edge
# is what makes the rule work on live data instead of only on history.
#
# It is bounded on purpose. When a direct feed stops, the relay becomes visible again
# this much later rather than immediately, which is a few minutes of duplicate rather
# than a silent gap. Sessions get the wider value because whole-night boundaries move
# further between routes; it is the same tolerance device detection uses to decide two
# sessions are one event seen twice (app/services/devices/detection.py).
SAMPLE_EDGE_TOLERANCE = timedelta(minutes=5)
EVENT_EDGE_TOLERANCE = timedelta(minutes=20)
# An archived row is a whole day stamped at its start, and a daily score is one value
# for a calendar day however the provider timed it, so both cover a day at a time.
ARCHIVE_EDGE_TOLERANCE = timedelta(days=1)
SCORE_EDGE_TOLERANCE = timedelta(days=1)

# Beyond this many (brand, key, direct source) combinations a read stops measuring
# coverage per key and measures one span per brand instead. A read asking for every
# series type at once with several accounts connected would otherwise build a very
# wide statement; the coarser span is still correct, just less exact about a metric
# only the relay carries. In practice a read names one to three types.
_MAX_COVERAGE_PROBES = 48


def _span_of(
    timestamp_column: InstrumentedAttribute[datetime],
    data_source_id_column: "InstrumentedAttribute[UUID] | InstrumentedAttribute[UUID | None]",
    source_id: UUID,
    extra_filters: list[ColumnElement[bool]],
    start: datetime | None,
    end: datetime | None,
) -> tuple[ColumnElement[datetime], ColumnElement[datetime]]:
    """Scalar min/max of one source's timestamps, as subqueries to embed in one SELECT.

    One source and one key per subquery rather than a ``GROUP BY`` over several: with
    the leading index columns pinned to single values Postgres answers min/max from the
    index endpoints, while the grouped form has to read every matching row.
    """
    conditions = [data_source_id_column == source_id, *extra_filters]
    if start is not None:
        conditions.append(timestamp_column >= start)
    if end is not None:
        conditions.append(timestamp_column < end)
    # correlate(None) so these stay self-contained: they are embedded in a SELECT with no
    # FROM of its own today, and an auto-correlation would silently change what they mean
    # if a caller ever embedded them somewhere that has one.
    return (
        select(func.min(timestamp_column)).where(*conditions).correlate(None).scalar_subquery(),
        select(func.max(timestamp_column)).where(*conditions).correlate(None).scalar_subquery(),
    )


def _measure_coverage(
    db_session: DbSession,
    timestamp_column: InstrumentedAttribute[datetime],
    data_source_id_column: "InstrumentedAttribute[UUID] | InstrumentedAttribute[UUID | None]",
    probes: list[tuple[tuple[str, int | str | None], UUID, list[ColumnElement[bool]]]],
    start: datetime | None,
    end: datetime | None,
    tolerance: timedelta,
) -> dict[tuple[str, int | str | None], tuple[datetime, datetime]]:
    """First and last timestamp the direct sources hold, per (brand, key), in one query.

    Every probe rides in a single SELECT so a read costs one round trip however many
    brands, types and accounts are involved. Each span is then widened by ``tolerance``
    at both ends, for the re-timestamping an aggregator does on its way through.
    """
    if not probes:
        return {}

    columns: list[ColumnElement[datetime]] = []
    for _key, source_id, extra_filters in probes:
        low, high = _span_of(timestamp_column, data_source_id_column, source_id, extra_filters, start, end)
        columns.extend((low, high))

    row = db_session.execute(select(*columns)).one()

    covered: dict[tuple[str, int | str | None], tuple[datetime, datetime]] = {}
    for index, (key, _source_id, _filters) in enumerate(probes):
        low, high = row[index * 2], row[index * 2 + 1]
        if low is None or high is None:
            continue
        known = covered.get(key)
        covered[key] = (min(low, known[0]), max(high, known[1])) if known else (low, high)
    return {key: (low - tolerance, high + tolerance) for key, (low, high) in covered.items()}


def _manual_spans(manual: list[DataSource]) -> tuple[list[_Span], list[HiddenRelay]]:
    if not manual:
        return [], []
    spans = [_Span(source_ids=tuple(s.id for s in manual), key=None, start=None, end=None)]
    hidden = [
        HiddenRelay(
            data_source_id=source.id,
            provider=_provider_slug(source.provider),
            brand=source.original_source_name or "",
            direct_provider=None,
            covered_from=None,
            covered_until=None,
            reason=REASON_MANUAL,
        )
        for source in manual
    ]
    return spans, hidden


def _plan(
    candidates: _Candidates,
    covered: dict[tuple[str, int | str | None], tuple[datetime, datetime]],
) -> RelayDedupPlan:
    """Assemble spans and their explanations from the coverage already measured."""
    spans, hidden = _manual_spans(candidates.manual)
    # Reported per relay, not per span: a read covering several series types produces a
    # span each, and quoting only the first type's window would understate what was left
    # out. The widest window across this relay's spans is what a reader needs.
    windows: dict[UUID, tuple[datetime, datetime]] = {}

    for (brand, key), (start, end) in covered.items():
        relays = candidates.relays.get(brand, [])
        if not relays:
            continue
        spans.append(_Span(source_ids=tuple(r.id for r in relays), key=key, start=start, end=end))
        for relay in relays:
            known = windows.get(relay.id)
            windows[relay.id] = (min(start, known[0]), max(end, known[1])) if known else (start, end)

    for brand, relays in candidates.relays.items():
        direct_provider = direct_provider_for_brand(brand)
        for relay in relays:
            window = windows.get(relay.id)
            if window is None:
                continue
            hidden.append(
                HiddenRelay(
                    data_source_id=relay.id,
                    provider=_provider_slug(relay.provider),
                    brand=relay.original_source_name or brand,
                    direct_provider=direct_provider.value if direct_provider else None,
                    covered_from=window[0],
                    covered_until=window[1],
                    reason=REASON_COVERED,
                )
            )

    return RelayDedupPlan(spans=tuple(spans), hidden=tuple(hidden))


@dataclass(frozen=True)
class RelayStatus:
    """Whether a source is a relayed second copy, for listings that show sources.

    Classification only - no timestamps, and so no coverage query. A source marked
    ``redundant`` is one reads will hide *where* the direct route covers it, which is
    not the same as "hidden everywhere": the span is resolved per read, against the
    window that read asks for.
    """

    redundant: bool
    direct_provider: str | None
    visibility: RelayVisibility


def classify_sources(db_session: DbSession, user_id: UUID) -> dict[UUID, RelayStatus]:
    """Per data source, whether the redundant-relay rule applies to it at all."""
    candidates = _candidates(db_session, user_id)
    statuses: dict[UUID, RelayStatus] = {}
    for brand, relays in candidates.relays.items():
        direct_provider = direct_provider_for_brand(brand)
        for relay in relays:
            statuses[relay.id] = RelayStatus(
                redundant=True,
                direct_provider=direct_provider.value if direct_provider else None,
                visibility=_visibility(relay),
            )
    for source in candidates.manual:
        statuses[source.id] = RelayStatus(
            redundant=True,
            direct_provider=None,
            visibility=RelayVisibility.NEVER,
        )
    return statuses


def build_series_plan(
    db_session: DbSession,
    user_id: UUID,
    type_ids: list[int],
    start: datetime | None = None,
    end: datetime | None = None,
    include_archive: bool = False,
) -> RelayDedupPlan:
    """What a sample read should leave out, resolved per series type.

    Per series type on purpose: a maker's own API and its aggregator copy rarely carry
    the same set of metrics, and a brand-wide rule would hide a stream the direct route
    never delivers. A type the direct sources hold nothing of in this window produces no
    span, so the relay keeps it.

    ``include_archive`` also measures coverage over the archived daily rows, and belongs
    to callers that read the archive as well - the activity summaries. A raw sample read
    must leave it off: archiving removes the direct rows from the live table, so counting
    archived coverage there would hide the relay over days whose direct data the read can
    no longer return, turning a duplicate into a hole.
    """
    candidates = _candidates(db_session, user_id)
    if not candidates.relays and not candidates.manual:
        return RelayDedupPlan()

    # A read that names no type asks for everything, and so does one naming more types
    # than _MAX_COVERAGE_PROBES allows. Both fall back to one span per brand: coarser,
    # since a type only the relay carries is then hidden inside it, but still bounded by
    # what the direct route delivered. Every timeseries read in practice names its types.
    per_type = bool(type_ids) and len(type_ids) * _direct_source_count(candidates) <= _MAX_COVERAGE_PROBES

    probes: list[tuple[tuple[str, int | str | None], UUID, list[ColumnElement[bool]]]] = []
    archive_probes: list[tuple[tuple[str, int | str | None], UUID, list[ColumnElement[bool]]]] = []
    for brand in candidates.relays:
        for direct in candidates.directs.get(brand, []):
            if per_type:
                for type_id in type_ids:
                    probes.append(((brand, type_id), direct.id, [DataPointSeries.series_type_definition_id == type_id]))
                    archive_probes.append(
                        ((brand, type_id), direct.id, [DataPointSeriesArchive.series_type_definition_id == type_id])
                    )
            else:
                probes.append(((brand, None), direct.id, []))
                archive_probes.append(((brand, None), direct.id, []))

    covered = _measure_coverage(
        db_session,
        DataPointSeries.recorded_at,
        DataPointSeries.data_source_id,
        probes,
        start,
        end,
        SAMPLE_EDGE_TOLERANCE,
    )
    if include_archive:
        covered = _widest(
            covered,
            _measure_coverage(
                db_session,
                DataPointSeriesArchive.bucket_start_at,
                DataPointSeriesArchive.data_source_id,
                archive_probes,
                start,
                end,
                # A day in the archive is one bucket stamped at its start, so the last
                # archived day covers the whole day, not just its first instant.
                ARCHIVE_EDGE_TOLERANCE,
            ),
        )
    return _plan(candidates, covered)


def _widest(
    left: dict[tuple[str, int | str | None], tuple[datetime, datetime]],
    right: dict[tuple[str, int | str | None], tuple[datetime, datetime]],
) -> dict[tuple[str, int | str | None], tuple[datetime, datetime]]:
    """One span per key, spanning both measurements."""
    merged = dict(left)
    for key, (low, high) in right.items():
        known = merged.get(key)
        merged[key] = (min(low, known[0]), max(high, known[1])) if known else (low, high)
    return merged


def _direct_source_count(candidates: "_Candidates") -> int:
    """Direct sources behind the brands that actually have a relay to weigh up."""
    return sum(len(candidates.directs.get(brand, [])) for brand in candidates.relays)


def build_event_plan(
    db_session: DbSession,
    user_id: UUID,
    categories: list[str],
    start: datetime | None = None,
    end: datetime | None = None,
) -> RelayDedupPlan:
    """What an event read (sleep, workouts, cycles) should leave out, per category.

    Events are matched on ``start_datetime`` alone. A session the direct route
    recorded and the relay re-timestamped by a few minutes still falls inside the
    covered span, which is what the span is for - the two copies never agree to the
    second.
    """
    candidates = _candidates(db_session, user_id)
    if not candidates.relays and not candidates.manual:
        return RelayDedupPlan()

    probes: list[tuple[tuple[str, int | str | None], UUID, list[ColumnElement[bool]]]] = [
        ((brand, category), direct.id, [EventRecord.category == category])
        for brand in candidates.relays
        for direct in candidates.directs.get(brand, [])
        for category in categories
    ]
    covered = _measure_coverage(
        db_session,
        EventRecord.start_datetime,
        EventRecord.data_source_id,
        probes,
        start,
        end,
        EVENT_EDGE_TOLERANCE,
    )
    return _plan(candidates, covered)


def build_score_plan(
    db_session: DbSession,
    user_id: UUID,
    categories: list[str],
    start: datetime | None = None,
    end: datetime | None = None,
) -> RelayDedupPlan:
    """What a score read (recovery, sleep score) should leave out, per score category.

    A score is one value for a day, and the two routes rarely stamp it at the same
    instant - a relayed recovery score can land hours from the direct one. The span
    therefore carries a day's tolerance rather than minutes: the unit being deduplicated
    is the day, not the timestamp.
    """
    candidates = _candidates(db_session, user_id)
    if not candidates.relays and not candidates.manual:
        return RelayDedupPlan()

    probes: list[tuple[tuple[str, int | str | None], UUID, list[ColumnElement[bool]]]] = [
        ((brand, category), direct.id, [HealthScore.category == category])
        for brand in candidates.relays
        for direct in candidates.directs.get(brand, [])
        for category in categories
    ]
    covered = _measure_coverage(
        db_session,
        HealthScore.recorded_at,
        HealthScore.data_source_id,
        probes,
        start,
        end,
        SCORE_EDGE_TOLERANCE,
    )
    return _plan(candidates, covered)


def relay_dedup_metadata(plan: "RelayDedupPlan | None") -> RelayDedupMetadata | None:
    """Report what the redundant-relay rule left out, or nothing when it left out nothing.

    Reported rather than silent: a stream that vanishes from a response has to be
    traceable to a decision, otherwise it reads as missing data and someone spends an
    afternoon on it.
    """
    if plan is None or not plan.applied:
        return None
    return RelayDedupMetadata(
        applied=True,
        hidden=[
            HiddenRelayInfo(
                data_source_id=entry.data_source_id,
                provider=entry.provider,
                brand=entry.brand or None,
                direct_provider=entry.direct_provider,
                covered_from=entry.covered_from,
                covered_until=entry.covered_until,
                reason=entry.reason,
            )
            for entry in plan.hidden
        ],
    )
