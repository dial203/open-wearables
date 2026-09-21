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
   themselves. Outside that span the relay is the only evidence and stays visible,
   so connecting Garmin today does not blank out the Apple-relayed Garmin history
   from before it was connected, and a direct feed that stops delivering hands
   visibility back to the relay without anyone touching a setting.

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
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_
from sqlalchemy.orm import InstrumentedAttribute

from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecord
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
        data_source_id_column: InstrumentedAttribute[UUID],
        *,
        key_column: InstrumentedAttribute | None = None,
        timestamp_column: InstrumentedAttribute[datetime] | None = None,
    ) -> list[ColumnElement[bool]]:
        """SQL saying "not one of the redundant rows", or nothing when there are none.

        One negated OR rather than a NOT IN per span: a row is kept unless it matches
        a span whole - source *and* key *and* timestamp - so a relay is filtered only
        where the direct route really covers it.
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
        return [~or_(*matches)] if matches else []


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


def _covered_span(
    db_session: DbSession,
    timestamp_column: InstrumentedAttribute[datetime],
    data_source_id_column: InstrumentedAttribute[UUID],
    source_ids: list[UUID],
    extra_filters: list[ColumnElement[bool]],
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """First and last timestamp the direct sources actually hold in this window.

    One query per source rather than a ``GROUP BY`` over all of them: with the source
    id and the series type pinned to single values, min/max are index endpoint lookups,
    while the grouped form has to scan every matching row. A participant has a handful
    of direct sources per brand, so the query count stays small and each one is cheap.
    """
    lows: list[datetime] = []
    highs: list[datetime] = []
    for source_id in source_ids:
        query = db_session.query(func.min(timestamp_column), func.max(timestamp_column)).filter(
            data_source_id_column == source_id,
            *extra_filters,
        )
        if start is not None:
            query = query.filter(timestamp_column >= start)
        if end is not None:
            query = query.filter(timestamp_column < end)
        low, high = query.one()
        if low is not None:
            lows.append(low)
        if high is not None:
            highs.append(high)
    return (min(lows) if lows else None, max(highs) if highs else None)


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
    reported: set[UUID] = set()

    for (brand, key), (start, end) in covered.items():
        relays = candidates.relays.get(brand, [])
        if not relays:
            continue
        spans.append(_Span(source_ids=tuple(r.id for r in relays), key=key, start=start, end=end))
        direct_provider = direct_provider_for_brand(brand)
        for relay in relays:
            if relay.id in reported:
                continue
            reported.add(relay.id)
            hidden.append(
                HiddenRelay(
                    data_source_id=relay.id,
                    provider=_provider_slug(relay.provider),
                    brand=relay.original_source_name or brand,
                    direct_provider=direct_provider.value if direct_provider else None,
                    covered_from=start,
                    covered_until=end,
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
) -> RelayDedupPlan:
    """What a sample read should leave out, resolved per series type.

    Per series type on purpose: a maker's own API and its aggregator copy rarely carry
    the same set of metrics, and a brand-wide rule would hide a stream the direct route
    never delivers. A type the direct sources hold nothing of in this window produces no
    span, so the relay keeps it.
    """
    candidates = _candidates(db_session, user_id)
    if not candidates.relays and not candidates.manual:
        return RelayDedupPlan()

    covered: dict[tuple[str, int | str | None], tuple[datetime, datetime]] = {}
    for brand in candidates.relays:
        direct_ids = [d.id for d in candidates.directs.get(brand, [])]
        if not direct_ids:
            continue
        if not type_ids:
            # A read that names no type asks for everything, and measuring coverage for
            # every series type would cost a query per type per source. One span across
            # all of them is coarser - a type only the relay carries is hidden inside it -
            # so callers that care name their types, which every timeseries read does.
            low, high = _covered_span(
                db_session,
                DataPointSeries.recorded_at,
                DataPointSeries.data_source_id,
                direct_ids,
                [],
                start,
                end,
            )
            if low is not None and high is not None:
                covered[(brand, None)] = (low, high)
            continue
        for type_id in type_ids:
            low, high = _covered_span(
                db_session,
                DataPointSeries.recorded_at,
                DataPointSeries.data_source_id,
                direct_ids,
                [DataPointSeries.series_type_definition_id == type_id],
                start,
                end,
            )
            if low is not None and high is not None:
                covered[(brand, type_id)] = (low, high)

    return _plan(candidates, covered)


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

    covered: dict[tuple[str, int | str | None], tuple[datetime, datetime]] = {}
    for brand in candidates.relays:
        direct_ids = [d.id for d in candidates.directs.get(brand, [])]
        if not direct_ids:
            continue
        for category in categories:
            low, high = _covered_span(
                db_session,
                EventRecord.start_datetime,
                EventRecord.data_source_id,
                direct_ids,
                [EventRecord.category == category],
                start,
                end,
            )
            if low is not None and high is not None:
                covered[(brand, category)] = (low, high)

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
