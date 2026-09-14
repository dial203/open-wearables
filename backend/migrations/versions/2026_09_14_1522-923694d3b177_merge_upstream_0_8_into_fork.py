"""merge upstream 0.8 into fork

Upstream's 9b079dab9585..a7c3e9f1b2d4 chain (workout detail columns, SyncRun
tracking, hashed API keys) and this fork's merge point 92b3c62b7c00 both descend
from b2c3d4e5f6a1, leaving two alembic heads after the sync. This joins them so
there is a single head again.

No schema work of its own: upstream's chain touches workout_details, sync_run and
api_key, the fork branch touches data_source/user_connection, so either order is
valid.

Generated with ``alembic merge heads`` — see backend/AGENTS.md for why a fork sync
adds a merge revision rather than re-pointing an existing one.

Revision ID: 923694d3b177
Revises: 92b3c62b7c00, a7c3e9f1b2d4

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "923694d3b177"
down_revision: Union[str, None] = ("92b3c62b7c00", "a7c3e9f1b2d4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
