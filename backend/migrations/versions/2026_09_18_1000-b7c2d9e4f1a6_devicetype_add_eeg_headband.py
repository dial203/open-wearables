"""add eeg and headband to the devicetype enum

Revision ID: b7c2d9e4f1a6
Revises: d3f1a8c2e5b4

device_type_priority.device_type is a native PostgreSQL enum (``devicetype``)
holding the Python enum *names*, so adding DeviceType.EEG and DeviceType.HEADBAND
in application code is not enough - the database type has to learn the labels too,
otherwise seeding their priorities fails with:

    invalid input value for enum devicetype: "EEG"

(device.device_type and data_source.device_type are plain VARCHARs and are
unaffected; see the note in app/models/device.py about why.)

ALTER TYPE ... ADD VALUE is allowed inside a transaction on PostgreSQL 12+; the new
labels just cannot be *used* until that transaction commits, which is fine here
because the priority seeding runs as a later step at application startup.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c2d9e4f1a6"
down_revision: Union[str, None] = "d3f1a8c2e5b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE devicetype ADD VALUE IF NOT EXISTS 'EEG'")
    op.execute("ALTER TYPE devicetype ADD VALUE IF NOT EXISTS 'HEADBAND'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type without recreating it.
    # Leaving the labels in place is harmless: nothing references them once the
    # corresponding priority rows are gone.
    pass
