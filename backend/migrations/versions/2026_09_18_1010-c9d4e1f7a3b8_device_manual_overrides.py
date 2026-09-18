"""device: hand-set brand, inventory fields, and the relaying host's model

Revision ID: c9d4e1f7a3b8
Revises: b7c2d9e4f1a6

Four nullable columns on ``device``. Nothing existing is altered or dropped.

``brand_display`` completes the pair ``model_display`` already started: ``brand`` and
``model_raw`` record what the provider's report resolved to and stay immutable, so a
correction needs somewhere to live that is not on top of the evidence. A relayed
stream is the common case - Apple Health carrying another maker's data resolves to
the platform's brand far more often than to the maker's.

``serial`` and ``firmware_version`` are inventory fields no provider in this system
reports. ``serial`` is not an identity claim and groups nothing (see the column
comment in app/models/device.py); firmware is here because it changes a device's
behaviour without changing any identifier, which matters when a measurement series
spans an update.

``host_model_raw`` is the phone that relayed a stream. Third-party apps writing into
HealthKit generally pass no HKDevice, so the only model string on those rows names
the phone that ran the app. Keeping it in its own column is what lets detection stop
treating it as the unit's model - see the accompanying change to
app/services/devices/detection.py, and scripts/data_migrations/split_host_relayed_devices.py
for the rows already grouped that way.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d4e1f7a3b8"
down_revision: Union[str, None] = "b7c2d9e4f1a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("device", sa.Column("brand_display", sa.String(length=64), nullable=True))
    op.add_column("device", sa.Column("serial", sa.String(length=100), nullable=True))
    op.add_column("device", sa.Column("firmware_version", sa.String(length=64), nullable=True))
    op.add_column("device", sa.Column("host_model_raw", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("device", "host_model_raw")
    op.drop_column("device", "firmware_version")
    op.drop_column("device", "serial")
    op.drop_column("device", "brand_display")
