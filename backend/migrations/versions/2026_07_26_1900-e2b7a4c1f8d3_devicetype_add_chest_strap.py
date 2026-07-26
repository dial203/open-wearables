"""add chest_strap to the devicetype enum

Revision ID: e2b7a4c1f8d3
Revises: a1f4c7e9d3b2

device_type_priority.device_type is a native PostgreSQL enum (``devicetype``)
holding the Python enum *names*, so adding DeviceType.CHEST_STRAP in application
code is not enough — the database type has to learn the label too, otherwise
seeding the new priority fails with:

    invalid input value for enum devicetype: "CHEST_STRAP"

(data_source.device_type is a plain VARCHAR and is unaffected.)

ALTER TYPE ... ADD VALUE is allowed inside a transaction on PostgreSQL 12+; the
new label just cannot be *used* until that transaction commits, which is fine
here because the priority seeding runs as a later step.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2b7a4c1f8d3"
down_revision: Union[str, None] = "a1f4c7e9d3b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE devicetype ADD VALUE IF NOT EXISTS 'CHEST_STRAP'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum type without recreating it.
    # Leaving the label in place is harmless: nothing references it after the
    # corresponding priority row is removed.
    pass
