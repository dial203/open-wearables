from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.orm import Mapped

from app.database import BaseDbModel
from app.mappings import FKDevice, FKUser, PrimaryKey, json_object, numeric_5_2, str_32, str_128


class DeviceLinkProposal(BaseDbModel):
    """A suggestion that two devices are the same physical unit seen by different routes.

    No identifier is shared across ingest routes, so cross-route identity cannot be
    resolved by matching ids. It is inferred from evidence - brand and device type
    agreement, firmware agreement where both routes expose it, and above all temporal
    overlap of the same measurement, since one ring cannot produce two genuinely
    different sleep sessions on the same night.

    That inference is never applied on its own. A wrong split is visible and
    reversible; a wrong merge silently pools two units' samples into one stream and
    may not be noticed until it is in an analysis, at which point the samples cannot
    be separated again. So the scorer writes a proposal and a person decides.

    Rejected rows are kept forever - they are what stops the scorer proposing the
    same pair on every run.
    """

    __tablename__ = "device_link_proposal"
    __table_args__ = (
        # One proposal per unordered pair. Callers order the ids before writing, which
        # the constraint below enforces, so (a,b) and (b,a) cannot both exist.
        Index("uq_device_link_proposal_pair", "device_a_id", "device_b_id", unique=True),
        Index("ix_device_link_proposal_user_status", "user_id", "status"),
        CheckConstraint("device_a_id < device_b_id", name="ck_device_link_proposal_ordered"),
    )

    id: Mapped[PrimaryKey[UUID]]
    user_id: Mapped[FKUser]
    device_a_id: Mapped[FKDevice]
    device_b_id: Mapped[FKDevice]

    # 0-100. Deliberately not a threshold that auto-accepts at some value: the score
    # orders the queue for a human, it does not stand in for one.
    score: Mapped[numeric_5_2]
    # What produced the score, kept so a reviewer can judge the proposal rather than
    # trust it: nights compared, overlap fraction, which signals agreed.
    evidence: Mapped[json_object | None]

    status: Mapped[str_32]  # LinkProposalStatus
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str_128 | None]

    updated_at: Mapped[datetime]
