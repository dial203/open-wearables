"""merge upstream telemetry into fork

Upstream's ef6ff24def41 (telemetry_state, #1695) descends from a7c3e9f1b2d4, as
does this fork's chain ending at f3a9c1e7b2d4, leaving two alembic heads after
the sync. This joins them so there is a single head again.

No schema work of its own: upstream's revision only creates the singleton
telemetry_state table, which no fork table references, so either order is valid.

Generated with ``alembic merge heads`` - see backend/AGENTS.md for why a fork sync
adds a merge revision rather than re-pointing an existing one.

Revision ID: 2582d5fb2ffb
Revises: ef6ff24def41, f3a9c1e7b2d4

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "2582d5fb2ffb"
down_revision: Union[str, None] = ("ef6ff24def41", "f3a9c1e7b2d4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
