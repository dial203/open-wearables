"""merge upstream 0.7.0 into fork

Upstream's b2c3d4e5f6a1 (remove event_record_detail) and this fork's
b7e3c1a9d2f4..e2b7a4c1f8d3 chain both branch off 7d6921a86914, leaving two
alembic heads after the merge. This joins them so there is a single head again.

No schema work of its own: the two branches touch different tables, so either
order is valid. A database already at e2b7a4c1f8d3 picks up b2c3d4e5f6a1 on the
next upgrade.

Revision ID: f1a2b3c4d5e6
Revises: b2c3d4e5f6a1, e2b7a4c1f8d3

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = ("b2c3d4e5f6a1", "e2b7a4c1f8d3")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
