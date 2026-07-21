"""backfill data_source original_source_name (canonical brand)

Revision ID: b7e3c1a9d2f4
Revises: 7d6921a86914

Non-destructive data backfill: populate original_source_name with a canonical
brand for existing data_source rows that don't already have one, using the same
resolver applied to new rows on ingest (app.utils.device_registry.resolve_brand).
device_model is never modified. On a fresh database this runs against an empty
table and is a no-op; new rows are tagged live via ensure_data_source().
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.schemas.enums import ProviderName
from app.utils.device_registry import resolve_brand

# revision identifiers, used by Alembic.
revision: str = "b7e3c1a9d2f4"
down_revision: Union[str, None] = "7d6921a86914"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, provider, device_model, source FROM data_source "
            "WHERE original_source_name IS NULL"
        )
    ).fetchall()
    for row in rows:
        try:
            provider = ProviderName(row.provider)
        except ValueError:
            provider = ProviderName.UNKNOWN
        brand = resolve_brand(provider, row.device_model, row.source)
        if brand:
            conn.execute(
                sa.text("UPDATE data_source SET original_source_name = :brand WHERE id = :id"),
                {"brand": brand, "id": row.id},
            )


def downgrade() -> None:
    # Data-only backfill. These rows had original_source_name = NULL beforehand,
    # but backfilled values are not distinguishable from provider-supplied ones,
    # so this is intentionally a no-op.
    pass
