"""Unfreeze the Ultra 2's name: drop the "Series 8" detection copied onto it.

A second Apple table in app/utils/device_registry.py named "Watch7,5" - the Apple
Watch Ultra 2 - as a Series 8, and detection copied that name into model_display when
it created each device. The table is gone and detection no longer copies names in;
clearing the copies lets the hardware table name these devices when they are read.

Only the exact wrong pair is cleared: no person types "Apple Watch Series 8" for a
Watch7,5, so these are all detection's, and any other name was set on purpose.

Revision ID: c4e8a1d2f6b7
Revises: 7add2bd158e5
Create Date: 2026-10-06 16:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e8a1d2f6b7"
down_revision: Union[str, None] = "7add2bd158e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE device SET model_display = NULL WHERE model_raw = 'Watch7,5' AND model_display = 'Apple Watch Series 8'"
    )


def downgrade() -> None:
    # Nothing to restore: the cleared value was wrong, and the name the hardware table
    # now gives these devices is the one a downgrade would have to recreate by hand.
    pass
