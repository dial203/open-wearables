"""user_connection device_label

Revision ID: c4e8f1a2b9d7
Revises: b7e3c1a9d2f4

Adds a device_label column to user_connection: a device identifier for the
hardware behind a connection (e.g. "Whoop 5.0", "Oura Ring Gen3"), used to fill
data_source.device_model for providers whose API doesn't report a device.
Nullable and additive; existing rows are unaffected.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8f1a2b9d7"
down_revision: Union[str, None] = "b7e3c1a9d2f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_connection", sa.Column("device_label", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("user_connection", "device_label")
