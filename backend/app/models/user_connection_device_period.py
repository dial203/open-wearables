from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import BaseDbModel
from app.mappings import PrimaryKey, str_100


class UserConnectionDevicePeriod(BaseDbModel):
    """One stretch of an account's dated device history: this device, from this instant.

    ``user_connection.device_label`` says what device is behind an account and never
    when. That is enough until a person changes watch on an account whose provider
    names no device - Garmin's wellness summaries, Whoop, most rings - and then every
    night after the switch is filed under the old model with nothing on the row to
    show it. This table is the "when": an account's periods, each running from its
    ``effective_from`` until the next one's.

    ``effective_from`` NULL means "from the start of this account's data", and there
    is at most one such row per account. A timestamp before the earliest period with
    no open-start row is covered by nothing, and resolves to no label rather than to
    the nearest period: an unstated stretch is left unattributed, never guessed.

    It is a statement, not a capture. It only ever fills a device the provider did
    not report; a model the provider stamped on a record always wins over it. See
    app/utils/device_timeline.py.
    """

    __tablename__ = "user_connection_device_period"
    __table_args__ = (
        # One period per instant per account, and at most one open start: NULLS NOT
        # DISTINCT makes two NULL effective_from rows collide the way two equal
        # timestamps do, which a plain unique index would let through.
        Index(
            "uq_user_connection_device_period_start",
            "user_connection_id",
            "effective_from",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[PrimaryKey[UUID]]
    # CASCADE: a period describes one account, and outliving it would leave a history
    # nobody can address. The data it labelled is untouched - its data sources keep
    # their device_model, and their connection link is SET NULL as it always was.
    user_connection_id: Mapped[UUID] = mapped_column(ForeignKey("user_connection.id", ondelete="CASCADE"))
    device_label: Mapped[str_100]
    effective_from: Mapped[datetime | None]
