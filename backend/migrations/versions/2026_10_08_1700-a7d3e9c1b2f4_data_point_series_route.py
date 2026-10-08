"""Record which feed wrote each time-series row: data_point_series.route_id.

One provider can have several feeds for one series that resolve to the same data
source - a Garmin watch's per-second workout heart rate and its all-day monitoring -
and the row key (source, type, second) could not tell them apart, so the last feed to
arrive owned every second they shared. route_id names the feed (ids in
app/schemas/enums/sample_route.py) and lets the upsert rank them.

Nullable with no default, so adding it does not rewrite the table: existing rows read
as "route unknown", which is what they are, and pick a route up the next time their
feed delivers them again.

Revision ID: a7d3e9c1b2f4
Revises: f4c24463e549
Create Date: 2026-10-08 17:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7d3e9c1b2f4"
down_revision: Union[str, None] = "f4c24463e549"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("data_point_series", sa.Column("route_id", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("data_point_series", "route_id")
