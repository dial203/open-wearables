"""Cross-route linking is proposed from evidence and never applied automatically.

No identifier is shared across ingest routes, so "is this the same ring?" is answered
by temporal overlap of the same measurement - one ring cannot produce two genuinely
different sleep sessions on the same night. That inference is good enough to put a
pair in front of a person and deliberately not good enough to act on, because it
cannot distinguish "one ring, two routes" from "two identical rings worn alternately".
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource, EventRecord, User
from app.repositories.data_source_repository import DataSourceRepository
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import ProviderName
from app.services.devices.detection import PROPOSAL_MIN_SCORE, DeviceDetectionService


@pytest.fixture
def user(db: Session) -> User:
    user = User(id=uuid4(), external_user_id=f"sub-{uuid4().hex[:8]}")
    db.add(user)
    db.flush()
    return user


def _ensure(db: Session, user: User, provider: ProviderName, model: str, source: str) -> DataSource:
    return DataSourceRepository().ensure_data_source(
        db, user_id=user.id, provider=provider, device_model=model, source=source
    )


def _add_sleep(db: Session, data_source: DataSource, nights: list[datetime], drift: timedelta = timedelta()) -> None:
    for start in nights:
        shifted = start + drift
        db.add(
            EventRecord(
                id=uuid4(),
                data_source_id=data_source.id,
                category="sleep",
                type="sleep_session",
                source_name="test",
                start_datetime=shifted,
                end_datetime=shifted + timedelta(hours=7),
                duration_seconds=7 * 3600,
            )
        )
    db.flush()


def _nights(count: int, step_days: int = 1) -> list[datetime]:
    base = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    return [base + timedelta(days=i * step_days) for i in range(count)]


class TestScoring:
    def test_overlapping_sessions_on_two_routes_are_proposed(self, db: Session, user: User) -> None:
        """The same ring, read directly and relayed through Apple Health.

        The relay re-timestamps and re-rounds, so the sessions are minutes apart
        rather than identical - which is why matching allows a tolerance.
        """
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        relayed = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        nights = _nights(20)
        _add_sleep(db, direct, nights)
        _add_sleep(db, relayed, nights, drift=timedelta(minutes=4))

        written = DeviceDetectionService().propose_cross_route_links(db, user.id)

        assert len(written) == 1
        assert written[0]["score"] >= PROPOSAL_MIN_SCORE
        assert written[0]["evidence"]["overlap_fraction"] == 1.0
        # Proposed, not linked: the two data sources keep their own devices.
        assert direct.device_id != relayed.device_id

    def test_two_rings_worn_on_alternate_nights_are_not_proposed(self, db: Session, user: User) -> None:
        """Same brand, same model, no shared nights - two units, not one seen twice."""
        first = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        second = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        _add_sleep(db, first, _nights(10, step_days=2))
        _add_sleep(db, second, [n + timedelta(days=1) for n in _nights(10, step_days=2)])

        assert DeviceDetectionService().propose_cross_route_links(db, user.id) == []

    def test_too_few_shared_nights_is_reported_not_scored(self, db: Session, user: User) -> None:
        """Two aligned sessions is coincidence, not evidence.

        Scoring it anyway would teach a reviewer that the evidence block is noise.
        """
        first = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        second = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        nights = _nights(2)
        _add_sleep(db, first, nights)
        _add_sleep(db, second, nights)

        assert DeviceDetectionService().propose_cross_route_links(db, user.id) == []

    def test_different_brands_are_never_candidates(self, db: Session, user: User) -> None:
        """A watch and a ring worn the same nights overlap perfectly and are not one device."""
        garmin = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        oura = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        nights = _nights(20)
        _add_sleep(db, garmin, nights)
        _add_sleep(db, oura, nights)

        assert DeviceDetectionService().propose_cross_route_links(db, user.id) == []

    def test_devices_sharing_a_route_are_skipped(self, db: Session, user: User) -> None:
        """Within-route rules already ruled on these: if they kept them apart, they are two."""
        first = _ensure(db, user, ProviderName.GARMIN, "fenix 8", "garmin")
        second = _ensure(db, user, ProviderName.GARMIN, "fenix 7", "garmin")
        nights = _nights(20)
        _add_sleep(db, first, nights)
        _add_sleep(db, second, nights)

        assert DeviceDetectionService().propose_cross_route_links(db, user.id) == []

    def test_a_short_history_is_not_diluted_by_a_long_one(self, db: Session, user: User) -> None:
        """A 20-night ring fully contained in a 200-night stream is a strong match.

        Scoring against the larger side would read it as 10% and hide a real link.
        """
        short = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        long = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        all_nights = _nights(200)
        _add_sleep(db, short, all_nights[:20])
        _add_sleep(db, long, all_nights)

        written = DeviceDetectionService().propose_cross_route_links(db, user.id)
        assert len(written) == 1
        assert written[0]["evidence"]["overlap_fraction"] == 1.0
        assert written[0]["evidence"]["sessions_compared"] == 20


class TestProposalLifecycle:
    def test_rerunning_does_not_revive_a_rejected_pair(self, db: Session, user: User) -> None:
        direct = _ensure(db, user, ProviderName.OURA, "Oura Ring Gen3", "oura")
        relayed = _ensure(db, user, ProviderName.APPLE, "Oura Ring Gen3", "com.ouraring.oura")
        nights = _nights(20)
        _add_sleep(db, direct, nights)
        _add_sleep(db, relayed, nights)

        detector = DeviceDetectionService()
        repo = DeviceRepository()

        assert len(detector.propose_cross_route_links(db, user.id)) == 1
        repo.decide_proposal(db, repo.pending_proposals(db, user.id)[0], accepted=False, actor="dial@osu.edu")

        assert detector.propose_cross_route_links(db, user.id) == []
        assert repo.pending_proposals(db, user.id) == []
