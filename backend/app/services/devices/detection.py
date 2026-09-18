"""Decide which physical device a data source belongs to.

The rule this module exists to enforce: **over-split, never over-merge.**

A wrong split is visible and reversible - an extra device appears in the list and
someone merges it. A wrong merge silently pools two units' samples into one stream,
produces no symptom, and may not be noticed until the data is in an analysis, at
which point the samples cannot be separated again. Every ambiguity here therefore
resolves toward "these are two devices until a person says otherwise".

Concretely:

- Only a STRONG identity claim - provider-issued, stable for the life of the unit,
  different for two same-model units owned by one person - may group data sources
  on its own.
- A WEAK claim groups only within a single route, and only on an exact match of the
  provider's own model string. That reproduces what a provider already asserts
  ("these rows came from a fenix 8") without inventing anything.
- Nothing groups across routes automatically. The same ring seen through Oura's API
  and through Apple Health shares no identifier, so the link is inferred from
  evidence and confirmed by a person. See ``propose_cross_route_links``.
- Two strong claims that resolve to different devices are not reconciled here. They
  become a proposal, because a claim that has moved is either a re-paired device or
  a genuine second unit and only a human can tell which.
"""

from datetime import datetime, timedelta
from logging import getLogger
from uuid import UUID

from sqlalchemy import select

from app.database import DbSession
from app.models import DataSource, Device, EventRecord
from app.repositories.device_repository import SYSTEM_ACTOR, DeviceRepository
from app.schemas.enums import (
    STRONG_IDENTITY_KINDS,
    WRITER_IDENTITY_KINDS,
    DeviceIdentityKind,
    DeviceType,
    IdentityConfidence,
    LabelSource,
    ProviderName,
    infer_device_type_from_source_name,
)
from app.services.devices.identity import IdentityClaim, claims_from_data_source, relaying_host_model
from app.utils.device_registry import humanize_device_model, relayed_brand, resolve_brand

log = getLogger(__name__)

# A proposal is worth a person's attention at or above this score. Not an
# auto-accept threshold - there is deliberately no score at which the system links
# two devices by itself, because the cost of being wrong is asymmetric and the
# evidence available (brand, type, session overlap) cannot distinguish "the same
# ring by two routes" from "two identical rings worn on alternate nights".
PROPOSAL_MIN_SCORE = 60.0

# Two sessions this close are treated as the same event seen twice. Aggregator
# relays re-timestamp and re-round, so an exact match is too strict; a wider window
# starts matching genuinely different sessions on the same night.
SESSION_MATCH_TOLERANCE = timedelta(minutes=20)


class DeviceDetectionService:
    def __init__(self, repo: DeviceRepository | None = None):
        self.repo = repo or DeviceRepository()

    def resolve_for_data_source(
        self,
        db_session: DbSession,
        data_source: DataSource,
        extra_claims: list[IdentityClaim] | None = None,
        actor: str = SYSTEM_ACTOR,
    ) -> Device | None:
        """Attribute a data source to a device, creating one if warranted.

        Returns None when there is nothing to go on - a provider that reports no
        device and no identifying claim. That is a normal resting state, not a
        failure: the source stays unattributed until either a later sync carries a
        signal or a person links it by hand. Inventing a device from an empty
        signal would produce a registry entry that looks like evidence.
        """
        provider = getattr(data_source.provider, "value", data_source.provider)
        claims = list(claims_from_data_source(provider, data_source.device_model, data_source.source))
        if extra_claims:
            claims.extend(extra_claims)
        if not claims:
            return None

        device, conflicts = self._resolve(db_session, data_source.user_id, provider, data_source, claims)
        if device is None:
            return None

        for claim in claims:
            if self.repo.add_claim(db_session, device, claim, actor=actor) is None:
                other = self.repo.find_by_claim(db_session, data_source.user_id, claim)
                if other is not None and other.id != device.id:
                    conflicts.add(other.id)

        # Re-attributing a data source that already belongs to another device is the
        # merge this whole module exists to avoid: it moves history from one unit to
        # another with no review and no way to see that it happened. Attribution is
        # therefore write-once for detection - only a person moves an attributed
        # source, through merge/split or an explicit link.
        already = data_source.device_id
        if already is not None and already != device.id:
            conflicts.add(device.id)
            device = self.repo.get(db_session, already) or device

        # A claim that now points somewhere else means either a re-paired device or a
        # second unit. Both are real possibilities and the data cannot tell them
        # apart, so nothing moves and the pair goes to a human.
        for other_id in conflicts:
            if other_id == device.id:
                continue
            self.repo.upsert_proposal(
                db_session,
                user_id=data_source.user_id,
                device_a=device.id,
                device_b=other_id,
                score=PROPOSAL_MIN_SCORE,
                evidence={"reason": "identity_claim_conflict", "route": provider},
                actor=actor,
            )

        self.repo.touch_seen(db_session, device)
        if already is None:
            self.repo.attach_data_source(
                db_session,
                data_source,
                device,
                actor=actor,
                reason="Auto-detected from provider identity claims",
            )
        return device

    def _resolve(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        data_source: DataSource,
        claims: list[IdentityClaim],
    ) -> tuple[Device | None, set[UUID]]:
        """Find the device these claims name, or create one. Returns (device, conflicts)."""
        conflicts: set[UUID] = set()

        # 1. Strong claims first. These are the only ones allowed to group on their own.
        strong = [c for c in claims if c.kind in STRONG_IDENTITY_KINDS and c.confidence is IdentityConfidence.STRONG]
        matched: dict[UUID, Device] = {}
        for claim in strong:
            found = self.repo.find_by_claim(db_session, user_id, claim)
            if found is not None:
                matched[found.id] = found

        if matched:
            # More than one strong claim resolving to different devices is exactly the
            # ambiguous case: pick deterministically (oldest wins, so repeated syncs
            # do not oscillate) and let the rest surface as a proposal.
            chosen = min(matched.values(), key=lambda d: (d.created_at, str(d.id)))
            conflicts.update(d_id for d_id in matched if d_id != chosen.id)
            return chosen, conflicts

        # The one case where the route's model string is not about this device at all:
        # a third-party app relaying through HealthKit or Health Connect reports the
        # phone that ran it. Every app on that phone reports the same string, so
        # grouping on it pools unrelated brands into one device.
        host_model = relaying_host_model(provider, data_source.device_model, data_source.source)

        # 2. Within-route match on the grouping key. Normally the provider's own model
        #    string, which reproduces what the provider itself asserts - "these rows
        #    came from a fenix 8" - and nothing more. Where the model names the relaying
        #    host, the writer id takes its place: it is the only value on the row that
        #    varies per device, so it splits what the model string would have merged.
        #    Either way the key is scoped to this route, so an identical value on
        #    another route never pulls the two together.
        kinds = WRITER_IDENTITY_KINDS if host_model is not None else {DeviceIdentityKind.MODEL_STRING}
        group_claim = next((c for c in claims if c.kind in kinds and c.route == provider), None)
        if group_claim is not None:
            found = self.repo.find_by_claim(db_session, user_id, group_claim)
            if found is not None:
                return found, conflicts

        # 3. Nothing matched. Create, but only when the route said something about the
        #    hardware: a device built purely from an app bundle id would be "whatever
        #    writes as com.ouraring.oura", which is a writer, not a unit. A relayed
        #    stream clears that bar - the platform did report hardware, just the wrong
        #    piece of it - and a source carrying no model and no host stays unattributed
        #    until a person links it, which is a normal resting state.
        if group_claim is None:
            return None, conflicts

        if host_model is not None:
            device = self._create_relayed(db_session, user_id, provider, data_source, host_model)
        else:
            device = self.repo.create(
                db_session,
                user_id=user_id,
                device_type=data_source.device_type or DeviceType.UNKNOWN.value,
                brand=resolve_brand(_provider_enum(provider), data_source.device_model, data_source.source),
                model_raw=data_source.device_model,
                model_display=humanize_device_model(data_source.device_model),
                actor=SYSTEM_ACTOR,
                reason=f"First seen on the {provider} route",
                detected=True,
            )
        return device, conflicts

    def _create_relayed(
        self,
        db_session: DbSession,
        user_id: UUID,
        provider: str,
        data_source: DataSource,
        host_model: str,
    ) -> Device:
        """Create the device behind a stream whose only model string named the relay host.

        Everything the platform reported about hardware describes the phone, so none of
        it is written where it would read as this device's own: ``model_raw`` stays
        NULL and the phone goes to ``host_model_raw``. What is left is the writing
        app's name, which is a weak but honest signal - it is the brand the person
        installed - and the device it produces is a starting point to correct rather
        than an answer. Correcting it is the point: ``model_display``, ``brand_display``
        and the type are all editable, and a hand-set label pins the device against
        later detection.
        """
        writer = data_source.source
        brand = relayed_brand(_provider_enum(provider), writer)

        # Not data_source.device_type: that was inferred from the host's model and says
        # "phone" for every relayed stream. The writer's name is the only description of
        # the hardware left, and UNKNOWN where it names nothing.
        device_type = infer_device_type_from_source_name(writer)

        return self.repo.create(
            db_session,
            user_id=user_id,
            device_type=device_type,
            brand=brand,
            model_raw=None,
            model_display=None,
            host_model_raw=host_model,
            # The writing app's name, as an auto label: it is what the person recognises
            # in the list, and being auto it never blocks a hand-set name.
            label=writer,
            label_source=LabelSource.AUTO,
            actor=SYSTEM_ACTOR,
            reason=f"Relayed through {provider} by {writer or 'an unnamed app'} on {host_model}",
            detected=True,
        )

    # --- cross-route proposals ---------------------------------------------------

    def propose_cross_route_links(
        self,
        db_session: DbSession,
        user_id: UUID,
        actor: str = SYSTEM_ACTOR,
    ) -> list[dict]:
        """Score pairs of devices that may be one unit seen by two routes.

        No identifier is shared across routes, so the evidence is indirect:

        - brand and device type agreement, which is necessary and nowhere near
          sufficient - a user can own two Oura rings;
        - temporal overlap of the same measurement, which is the real discriminator,
          because one ring cannot produce two genuinely different sleep sessions on
          the same night.

        Returns the proposals written, highest score first. Nothing is linked here.
        """
        devices = self.repo.list_for_user(db_session, user_id, include_retired=False)
        by_route: dict[UUID, set[str]] = {
            d.id: {c.route for c in self.repo.claims_for_device(db_session, d.id)} for d in devices
        }

        written: list[dict] = []
        for i, left in enumerate(devices):
            for right in devices[i + 1 :]:
                # Same route means the within-route rules already had their say; if they
                # kept these apart, they are two devices on that route.
                if by_route.get(left.id, set()) & by_route.get(right.id, set()):
                    continue
                score, evidence = self._score_pair(db_session, left, right)
                if score < PROPOSAL_MIN_SCORE:
                    continue
                proposal = self.repo.upsert_proposal(
                    db_session,
                    user_id=user_id,
                    device_a=left.id,
                    device_b=right.id,
                    score=score,
                    evidence=evidence,
                    actor=actor,
                )
                if proposal is not None:
                    written.append({"device_a": left.id, "device_b": right.id, "score": score, "evidence": evidence})

        written.sort(key=lambda p: p["score"], reverse=True)
        return written

    def _score_pair(self, db_session: DbSession, left: Device, right: Device) -> tuple[float, dict]:
        """Score two devices as candidates for the same unit, 0-100, with the evidence.

        Brand and type agreement are a gate, not a score: without them the pair is not
        a candidate at all. Session overlap carries the weight, because it is the only
        signal that separates "one unit, two routes" from "two units of the same model".
        """
        brand_match = bool(left.brand and right.brand and left.brand.casefold() == right.brand.casefold())
        type_match = left.device_type == right.device_type
        if not (brand_match and type_match):
            return 0.0, {"brand_match": brand_match, "type_match": type_match}

        overlap, compared = self._session_overlap(db_session, left, right)
        evidence = {
            "brand_match": True,
            "type_match": True,
            "sessions_compared": compared,
            "overlap_fraction": round(overlap, 3),
        }

        # Too few shared nights to mean anything. Say so rather than scoring on noise:
        # two sessions that happen to align is not evidence, and a proposal that claims
        # it is teaches the reviewer to stop reading the evidence.
        if compared < 3:
            evidence["note"] = "insufficient overlapping sessions to judge"
            return 0.0, evidence

        return round(40.0 + 60.0 * overlap, 2), evidence

    def _session_overlap(self, db_session: DbSession, left: Device, right: Device) -> tuple[float, int]:
        """Fraction of the rarer device's sessions that the other also recorded.

        Uses the rarer side as the denominator so a device with far more history does
        not dilute a real match: if every one of a 30-night ring's sessions appears in
        a 300-night stream, that is a strong signal, not a 10% one.
        """
        left_sessions = self._session_starts(db_session, left.id)
        right_sessions = self._session_starts(db_session, right.id)
        if not left_sessions or not right_sessions:
            return 0.0, 0

        smaller, larger = (
            (left_sessions, right_sessions)
            if len(left_sessions) <= len(right_sessions)
            else (right_sessions, left_sessions)
        )
        matches = sum(1 for start in smaller if any(abs(start - other) <= SESSION_MATCH_TOLERANCE for other in larger))
        return matches / len(smaller), len(smaller)

    @staticmethod
    def _session_starts(db_session: DbSession, device_id: UUID, limit: int = 400) -> list[datetime]:
        """Sleep session start times attributed to a device, newest first."""
        stmt = (
            select(EventRecord.start_datetime)
            .join(DataSource, DataSource.id == EventRecord.data_source_id)
            .where(DataSource.device_id == device_id, EventRecord.category == "sleep")
            .order_by(EventRecord.start_datetime.desc())
            .limit(limit)
        )
        return [row for row in db_session.scalars(stmt).all() if row is not None]


def _provider_enum(provider: str) -> ProviderName:
    try:
        return ProviderName(provider)
    except ValueError:
        return ProviderName.UNKNOWN
