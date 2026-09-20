"""classify a connected provider account: personal, validation, reliability, ...

Revision ID: c9a3e5b17d42
Revises: b5d41c7a9e02

The previous migration made it possible for one participant to hold several
accounts with the same provider, and recorded an e-mail so a data set could be
traced back to the account it came from. That answers *which* account; it does
not answer what the account is for, which is what a study actually filters on -
separating the validation arm from the participant's own everyday wear, or
keeping a device-checkout account out of an analysis entirely.

`account_type` is a plain nullable string, matching how data_source.device_type
and provider are stored, so the value set can grow without another migration.
The accepted values live in app/schemas/enums/account_type.py and are validated
at the API boundary.

No backfill and no default. Existing accounts come through unclassified, which
is the truthful state: nobody has said what they are for, and guessing
"personal" for a connection that turns out to be a validation unit would put a
wrong answer somewhere an analysis might trust it. The API reports unclassified
accounts as such so they can be found and corrected.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9a3e5b17d42"
down_revision: Union[str, None] = "b5d41c7a9e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("user_connection", sa.Column("account_type", sa.String(length=32), nullable=True))
    # Studies filter by what an account is for, across every participant at
    # once ("every validation account"), so the column is worth an index.
    op.create_index("ix_user_connection_account_type", "user_connection", ["account_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_user_connection_account_type", table_name="user_connection")
    op.drop_column("user_connection", "account_type")
