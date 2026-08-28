"""merge upstream health_score fk rename into fork

Upstream's dc5ac28c4b94 (health_score fk rename) branches off b2c3d4e5f6a1 —
the same parent as this fork's previous merge point f1a2b3c4d5e6 — so pulling
it in leaves two alembic heads. This joins them so there is a single head again.

No schema work of its own: dc5ac28c4b94 renames health_score foreign keys, the
fork branch touches data_source/user_connection, so either order is valid.

Revision ID: 92b3c62b7c00
Revises: f1a2b3c4d5e6, dc5ac28c4b94

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "92b3c62b7c00"
down_revision: Union[str, None] = ("f1a2b3c4d5e6", "dc5ac28c4b94")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
