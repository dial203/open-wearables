"""user_connection sensor_label

Revision ID: e1b7d4a9c3f5
Revises: d7f31a9c2b64

A sensor worn under the device the provider names.

Several providers report the device that *recorded* an activity, not the one that
sensed it. Strava's `device_name` is the watch or head unit that uploaded; a chest
strap paired to that watch appears nowhere in the payload, and `gear` covers bikes
and shoes only. Garmin's activity `deviceName` behaves the same way. So for the
common validation arrangement - an ECG strap as the reference, recorded through a
watch - the reference instrument is invisible to every field the API exposes, and
the data arrives attributed to the wrist.

Nothing can derive this: an activity with `has_heartrate` looks identical whether a
strap or a wrist optical sensor produced it. It is a declaration, which is why it is
a column a person fills rather than a signal something parses.

When set, the provider's reported model is treated as the *recorder* - it moves to
device.host_model_raw, the same place an aggregator's relaying handset goes - and
this label names the unit. Nothing is destroyed: data_source.device_model still holds
the provider's report verbatim.

Nullable and additive; existing rows are unaffected and behave exactly as before.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1b7d4a9c3f5"
down_revision: Union[str, None] = "d7f31a9c2b64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_connection", sa.Column("sensor_label", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("user_connection", "sensor_label")
