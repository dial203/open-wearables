"""user_connection_device_period, and data_source.device_model_origin

Revision ID: a8c4e2f6d913
Revises: f3a9c1e7b2d4

An account's dated device history. ``user_connection.device_label`` names the device
behind an account and never when, so a person who changes watch on an account whose
provider reports no device (Garmin's wellness summaries, Whoop) had every night after
the switch filed under the old model. Each row here is one stretch: this label, from
this instant, until the next row's. ``effective_from`` NULL is "from the start", and
NULLS NOT DISTINCT keeps it to one per account.

``data_source.device_model_origin`` records who put a source's device_model there -
the provider, or a label - because only a label-derived source may be re-filed by
date. It is written from now on and NOT backfilled: a model equal to today's label
could still have been the provider's own report, and a guessed origin would let a
re-file move data a provider stamped. NULL reads as "unrecorded", and a re-file
leaves those sources alone unless a person names them.

Additive; nothing existing changes behaviour until an account is given periods.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a8c4e2f6d913"
down_revision: Union[str, None] = "f3a9c1e7b2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_connection_device_period",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_connection.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("device_label", sa.String(length=100), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "uq_user_connection_device_period_start",
        "user_connection_device_period",
        ["user_connection_id", "effective_from"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.add_column("data_source", sa.Column("device_model_origin", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        "ck_data_source_device_model_origin",
        "data_source",
        "device_model_origin IS NULL OR (device_model IS NOT NULL AND device_model_origin IN ('provider', 'label'))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_data_source_device_model_origin", "data_source", type_="check")
    op.drop_column("data_source", "device_model_origin")
    op.drop_index("uq_user_connection_device_period_start", table_name="user_connection_device_period")
    op.drop_table("user_connection_device_period")
