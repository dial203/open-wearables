from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.database import DbSession
from app.models.device_type_priority import DeviceTypePriority
from app.schemas.enums import DEFAULT_DEVICE_TYPE_PRIORITY, DeviceType


class DeviceTypePriorityRepository:
    def get_all_ordered(self, db: DbSession) -> list[DeviceTypePriority]:
        stmt = select(DeviceTypePriority).order_by(DeviceTypePriority.priority)
        return list(db.scalars(stmt).all())

    def get_by_device_type(self, db: DbSession, device_type: DeviceType) -> DeviceTypePriority | None:
        stmt = select(DeviceTypePriority).where(DeviceTypePriority.device_type == device_type)
        return db.scalar(stmt)

    def get_priority_order(self, db: DbSession) -> dict[DeviceType, int]:
        priorities = self.get_all_ordered(db)
        if not priorities:
            return DEFAULT_DEVICE_TYPE_PRIORITY
        return {p.device_type: p.priority for p in priorities}

    def upsert(self, db: DbSession, device_type: DeviceType, priority: int) -> DeviceTypePriority:
        now = datetime.now(UTC)
        stmt = (
            insert(DeviceTypePriority)
            .values(id=uuid4(), device_type=device_type, priority=priority, created_at=now, updated_at=now)
            .on_conflict_do_update(
                index_elements=["device_type"],
                set_={"priority": priority, "updated_at": now},
            )
            .returning(DeviceTypePriority)
        )
        result = db.execute(stmt)
        db.flush()
        return result.scalar_one()

    def bulk_update(self, db: DbSession, priorities: list[tuple[DeviceType, int]]) -> list[DeviceTypePriority]:
        now = datetime.now(UTC)
        for device_type, priority in priorities:
            stmt = (
                insert(DeviceTypePriority)
                .values(id=uuid4(), device_type=device_type, priority=priority, created_at=now, updated_at=now)
                .on_conflict_do_update(
                    index_elements=["device_type"],
                    set_={"priority": priority, "updated_at": now},
                )
            )
            db.execute(stmt)
        db.flush()
        return self.get_all_ordered(db)

    def initialize_defaults(self, db: DbSession) -> list[DeviceTypePriority]:
        """Seed default priorities, adding any device types missing from the table.

        Additive on purpose: a device type introduced after a deployment was first
        seeded (e.g. chest_strap) must still reach an existing database, but rows an
        operator has already customised are left untouched.
        """
        existing = self.get_all_ordered(db)
        existing_types = {p.device_type for p in existing}
        missing = [(dt, pr) for dt, pr in DEFAULT_DEVICE_TYPE_PRIORITY.items() if dt not in existing_types]
        if not missing:
            return existing
        return self.bulk_update(db, missing)
