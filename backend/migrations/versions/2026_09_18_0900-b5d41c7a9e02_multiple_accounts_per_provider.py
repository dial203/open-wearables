"""multiple provider accounts per user, labelled and traceable to an e-mail

Revision ID: b5d41c7a9e02
Revises: d3f1a8c2e5b4

Until now a user could hold one account per provider: `ix_user_connection_user_provider`
was UNIQUE on (user_id, provider). That is wrong for validation and reliability work,
where one participant wears several units of the same brand at once - two Garmins, two
Whoops, two Ouras - each with its own account, its own tokens and its own data.

Three changes:

1. `user_connection` gains `account_label` and `account_email`. The label names the
   account for a human ("P01 left wrist"); the e-mail is the provenance record that says
   which login a data set came from, captured from the provider where its API exposes one
   (Whoop, Oura, Google Health) and entered by hand otherwise.

2. The unique index on (user_id, provider) is replaced by a plain index plus two partial
   unique indexes that keep the constraint that still matters: the *same external account*
   must not be linked twice to the same user. Once on provider_user_id, which is the
   provider's own identifier and the strongest evidence available, and once on the
   account e-mail for the providers that report no user id. Both are limited to active
   rows, so a revoked connection never blocks reconnecting.

3. `data_source`'s identity index gains user_connection_id. This is the change that
   actually separates the data: two accounts with one provider report the same
   device_model and the same source, so without the connection in the key both units'
   samples would land on one data_source and could not be told apart afterwards. NULL
   (one-time imports, XML) coalesces to the all-zero UUID because NULLs do not compare
   equal in a unique index.

Backfill: none needed. Existing connections keep working unchanged - a user with one
account per provider sees no difference - and their account_label/account_email stay NULL
until someone fills them in; the API falls back to provider_username and then to a
positional name so nothing is ever unlabelled on screen. Existing data_source rows keep
whatever user_connection_id they already had, and the new index accepts them as they are.

The downgrade recreates the UNIQUE (user_id, provider) index and will fail if any user
has by then connected a second account with one provider - which is the point of the
constraint it restores. Disconnect the extra accounts first.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5d41c7a9e02"
down_revision: Union[str, None] = "d3f1a8c2e5b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    # 1. Account identity columns.
    op.add_column("user_connection", sa.Column("account_label", sa.String(length=100), nullable=True))
    op.add_column("user_connection", sa.Column("account_email", sa.String(length=255), nullable=True))

    # 2. One account per provider -> one account per external identity.
    op.drop_index("ix_user_connection_user_provider", table_name="user_connection")
    op.create_index("ix_user_connection_user_provider", "user_connection", ["user_id", "provider"], unique=False)
    op.create_index(
        "uq_user_connection_user_provider_account",
        "user_connection",
        ["user_id", "provider", "provider_user_id"],
        unique=True,
        postgresql_where=sa.text("provider_user_id IS NOT NULL AND status = 'active'"),
    )
    op.create_index(
        "uq_user_connection_user_provider_email",
        "user_connection",
        ["user_id", "provider", sa.text("lower(account_email)")],
        unique=True,
        postgresql_where=sa.text("account_email IS NOT NULL AND status = 'active'"),
    )

    # 3. Data sources become per-account.
    op.drop_index("uq_data_source_identity", table_name="data_source")
    op.create_index(
        "uq_data_source_identity",
        "data_source",
        [
            "user_id",
            "provider",
            sa.text("COALESCE(device_model, '')"),
            sa.text("COALESCE(source, '')"),
            sa.text(f"COALESCE(user_connection_id, '{_ZERO_UUID}'::uuid)"),
        ],
        unique=True,
    )
    op.create_index("ix_data_source_user_connection", "data_source", ["user_connection_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_data_source_user_connection", table_name="data_source")
    op.drop_index("uq_data_source_identity", table_name="data_source")
    op.create_index(
        "uq_data_source_identity",
        "data_source",
        [
            "user_id",
            "provider",
            sa.text("COALESCE(device_model, '')"),
            sa.text("COALESCE(source, '')"),
        ],
        unique=True,
    )

    op.drop_index("uq_user_connection_user_provider_email", table_name="user_connection")
    op.drop_index("uq_user_connection_user_provider_account", table_name="user_connection")
    op.drop_index("ix_user_connection_user_provider", table_name="user_connection")
    # Fails when a user holds several accounts with one provider - deliberately.
    op.create_index("ix_user_connection_user_provider", "user_connection", ["user_id", "provider"], unique=True)

    op.drop_column("user_connection", "account_email")
    op.drop_column("user_connection", "account_label")
