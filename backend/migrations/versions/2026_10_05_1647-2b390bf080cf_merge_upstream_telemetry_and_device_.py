"""merge upstream telemetry and device types into fork

Upstream's ef6ff24def41 (telemetry_state, #1695) and a18e054b6e0f (extended device
types, #1729) descend from a7c3e9f1b2d4, as does this fork's chain ending at
a8c4e2f6d913, leaving two alembic heads after the sync. This joins them so there is a
single head again.

No schema work of its own, and either order is valid: upstream's revisions create the
singleton telemetry_state table and add devicetype enum values with ADD VALUE IF NOT
EXISTS - so CHEST_STRAP, which the fork added in e2b7a4c1f8d3, is a no-op - while the
fork's branch touches user_connection, data_source and the device registry.

Downgrade caution: a18e054b6e0f's downgrade rebuilds devicetype (the type of
device_type_priority.device_type) from upstream's original seven values, so on this
fork it fails while any device_type_priority row holds EEG or HEADBAND. Delete those
rows first if a downgrade past it is ever needed.

Generated with ``alembic merge heads`` - see backend/AGENTS.md for why a fork sync
adds a merge revision rather than re-pointing an existing one.

Revision ID: 2b390bf080cf
Revises: a18e054b6e0f, a8c4e2f6d913

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "2b390bf080cf"
down_revision: Union[str, None] = ("a18e054b6e0f", "a8c4e2f6d913")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
