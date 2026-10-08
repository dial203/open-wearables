"""merge upstream meal details into fork

Upstream's 4b9d28928ca1 (meal_details, nutrition #1674) descends from a18e054b6e0f, and
this fork's chain ending at c4e8a1d2f6b7 from 2b390bf080cf, the last sync's merge, which
already includes a18e054b6e0f. That leaves two alembic heads; this joins them.

No schema work of its own, and either order is valid: upstream's revision creates
meal_details and swaps event_record's (data_source_id, start, end) unique index for a
partial one that excludes meals; no fork revision on the other branch touches
event_record's indexes.

Generated with ``alembic merge heads`` - see backend/AGENTS.md for why a fork sync
adds a merge revision rather than re-pointing an existing one.

Revision ID: f4c24463e549
Revises: 4b9d28928ca1, c4e8a1d2f6b7

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "f4c24463e549"
down_revision: Union[str, None] = ("4b9d28928ca1", "c4e8a1d2f6b7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
