"""Record device identity claims carried by a mobile SDK sync payload.

HealthKit is the one route that sometimes ships a real device record. Apple's own
hardware populates HKDevice with a name, model, hardware and software version and a
``deviceId``; the SDK relays all of it in ``SourceInfo``. Until now only three of
those fields survived ingest - ``extract_device_info`` returns
(device_model, software_version, original_source_name) and everything else was
parsed and dropped, including ``deviceId``, which is the strongest per-unit signal
any route gives us for an Apple Watch.

This runs as a pass over a completed sync rather than inside the per-record loop.
The claims are per *source*, not per sample, so a payload with 40,000 heart-rate
records still describes two or three devices, and doing the work once per distinct
source keeps it off the hot path.
"""

from logging import getLogger
from typing import Any
from uuid import UUID

from app.database import DbSession
from app.models import DataSource
from app.repositories.device_repository import DeviceRepository
from app.schemas.enums import ProviderName
from app.services.devices.detection import DeviceDetectionService
from app.services.devices.identity import claims_from_sdk_source
from app.services.sdk.device_resolution import extract_device_info, extract_reported_device_type
from app.utils.connection_context import get_active_connection_id

log = getLogger(__name__)

ACTOR = "system:sdk_sync"


def record_sdk_identities(db_session: DbSession, user_id: UUID, provider: str, sources: list[Any]) -> int:
    """Attach each distinct SourceInfo's identity claims to its data source's device.

    Returns the number of data sources touched. Best effort throughout: identity
    enrichment must never fail a sync that has already stored its data, so a problem
    here is logged and the sync still reports success.
    """
    if not sources:
        return 0

    repo = DeviceRepository()
    detector = DeviceDetectionService(repo)
    seen: set[tuple[str | None, str | None]] = set()
    touched = 0

    try:
        for source in sources:
            if source is None:
                continue
            device_model, _software_version, original_source_name = extract_device_info(source)
            # The identity a data source is keyed by. ensure_data_source stores
            # `source = original_source_name` on the Apple path, so this matches how
            # the row was written rather than re-deriving a different key.
            key = (device_model, original_source_name)
            if key in seen:
                continue
            seen.add(key)

            claims = claims_from_sdk_source(provider, source)
            reported_device_type = extract_reported_device_type(source)
            # A source carrying neither an identity claim nor a platform-declared type
            # has nothing this pass can add. One carrying only the type still does:
            # resolve_for_data_source applies it before it decides there is nothing to
            # attribute, so the classification lands even though no device is created.
            if not claims and reported_device_type is None:
                continue

            data_source = _find_data_source(db_session, user_id, provider, device_model, original_source_name)
            if data_source is None:
                continue

            detector.resolve_for_data_source(
                db_session,
                data_source,
                extra_claims=claims,
                actor=ACTOR,
                reported_device_type=reported_device_type,
            )
            touched += 1

        db_session.commit()
    except Exception:
        db_session.rollback()
        log.exception("failed to record SDK device identities for user %s (provider=%s)", user_id, provider)
        return 0

    return touched


def _find_data_source(
    db_session: DbSession,
    user_id: UUID,
    provider: str,
    device_model: str | None,
    source: str | None,
) -> DataSource | None:
    try:
        provider_enum = ProviderName(provider)
    except ValueError:
        return None
    from app.repositories.data_source_repository import DataSourceRepository

    repo = DataSourceRepository()
    # Inside a sync scoped to one account (the normal SDK path) the identity is
    # only meaningful together with that connection: a user with two phones on
    # two Apple IDs has two data sources per identity, and attributing both to
    # the same device would merge the units.
    connection_id = get_active_connection_id()
    if connection_id is not None:
        return repo.get_by_connection_identity(db_session, user_id, provider_enum, device_model, source, connection_id)
    return repo.get_by_identity(db_session, user_id, provider_enum, device_model, source)
