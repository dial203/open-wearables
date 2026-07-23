"""workout_details fit_file_key

Revision ID: a1f4c7e9d3b2
Revises: c4e8f1a2b9d7

Adds a fit_file_key column to workout_details: the storage key of the raw FIT
file retained for a workout (when STORE_FIT_FILES is enabled). Lets the workout
FIT-download endpoint locate the file. Nullable and additive; existing rows are
unaffected.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f4c7e9d3b2"
down_revision: Union[str, None] = "c4e8f1a2b9d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("workout_details", sa.Column("fit_file_key", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("workout_details", "fit_file_key")
