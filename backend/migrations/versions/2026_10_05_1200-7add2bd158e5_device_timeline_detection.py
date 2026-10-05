"""Detected device history: period origin, and per-account detection state

Revision ID: 7add2bd158e5
Revises: a8c4e2f6d913

An account's dated device history was only ever typed by hand, so the dialog offered
today's device "from the start" even on a Garmin account whose activities name every
watch it was worn with. Detection now reads those names (app/utils/device_switch_detection.py).

- ``user_connection_device_period.origin``: 'detected' (read off the provider's own
  device names) or 'stated' (a person's). Existing rows were typed, so 'stated'.
- ``user_connection.device_timeline_auto``: whether detection maintains the history.
  On for every account; one whose provider names no device has nothing to detect.
- ``user_connection.device_timeline_detect_from``: detection only adds switches after
  this instant. An account that already has a history had it saved by someone, so it is
  set to when that happened - detection must not rewrite what a person stated - and the
  dashboard offers rebuilding it from the data as an explicit step.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7add2bd158e5"
down_revision: Union[str, None] = "a8c4e2f6d913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_connection_device_period",
        sa.Column("origin", sa.String(length=16), server_default=sa.text("'stated'"), nullable=False),
    )
    op.create_check_constraint(
        "ck_user_connection_device_period_origin",
        "user_connection_device_period",
        "origin IN ('stated', 'detected')",
    )
    op.add_column(
        "user_connection",
        sa.Column("device_timeline_auto", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "user_connection",
        sa.Column("device_timeline_detect_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE user_connection AS uc
        SET device_timeline_detect_from = saved.at
        FROM (
            SELECT user_connection_id,
                   GREATEST(MAX(created_at), COALESCE(MAX(effective_from), MAX(created_at))) AS at
            FROM user_connection_device_period
            GROUP BY user_connection_id
        ) AS saved
        WHERE saved.user_connection_id = uc.id
        """
    )


def downgrade() -> None:
    op.drop_column("user_connection", "device_timeline_detect_from")
    op.drop_column("user_connection", "device_timeline_auto")
    op.drop_constraint("ck_user_connection_device_period_origin", "user_connection_device_period", type_="check")
    op.drop_column("user_connection_device_period", "origin")
