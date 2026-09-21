"""per-source override of the redundant-relay rule

Revision ID: d7f31a9c2b64
Revises: c9a3e5b17d42

Someone who connects both a maker's API and an aggregator receives the maker's data
twice - an Oura ring read from Oura, and the same ring again as "Oura" inside Apple
Health. Reads now leave the aggregator's copy out for the span the direct route
actually covers (app/services/sources/relay_dedup.py).

Only the override is stored here. Redundancy itself is derived per read from the rows
present, because it depends on what the direct connection has really delivered: a
token that expires tonight, a backfill that never reached last winter, or an account
revoked next month each change the answer, and a stored flag would go on asserting
the old one. What no recomputation may undo is a person's decision, which is what
this column is.

'auto' for every existing row, which is the resting state and the only value the
system sets itself: follow the rule. 'always' keeps a source visible whatever the
rule says, 'never' hides it outright. Values are validated at the API boundary and
listed in app/schemas/enums/relay_visibility.py, stored as a plain string like
data_source.device_type and user_connection.account_type so the set can grow without
another migration.

Nothing is deleted and no ingest path changes: relayed rows keep being written, and
a read asking for them (include_redundant_relays=true) gets them back unchanged.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7f31a9c2b64"
down_revision: Union[str, None] = "c9a3e5b17d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "data_source",
        sa.Column("relay_visibility", sa.String(length=32), nullable=False, server_default="auto"),
    )


def downgrade() -> None:
    op.drop_column("data_source", "relay_visibility")
