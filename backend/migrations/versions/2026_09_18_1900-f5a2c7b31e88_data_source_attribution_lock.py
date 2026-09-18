"""data_source.attribution_locked_at: a detach that survives the next sync

Revision ID: f5a2c7b31e88
Revises: d3f1a8c2e5b4

One nullable timestamp. Detection skips a data source that carries it, and linking
the source to a device again clears it.

Why it is needed: attribution is write-once for a source that already has a device,
but a NULL device_id carries no history of its own. Detection reads it as "never
attributed" and re-attaches on the next batch, so unlinking by hand lasts exactly
until the following sync - the registry silently undoes a person's decision, and the
only trace is a pair of linked/unlinked rows in device_history that nothing reads.

Additive and nullable, so every existing row means "never deliberately detached",
which is the correct reading of the rows written before this column existed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a2c7b31e88"
down_revision: Union[str, None] = "d3f1a8c2e5b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "data_source",
        sa.Column("attribution_locked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_source", "attribution_locked_at")
