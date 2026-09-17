"""device registry: device, device_identity, device_history, device_link_proposal

Revision ID: d3f1a8c2e5b4
Revises: 923694d3b177

Adds a user-scoped registry of physical devices, sitting above data_source.

data_source stays exactly as it is: the immutable ingest fingerprint, one row per
(user, provider, device_model, source) as the provider reported it. The new device
table is the mutable, human-facing entity several data sources point at, which is
what lets a Garmin's activities, sleep and epoch streams read as one fenix, and an
Oura ring arriving both from Oura's API and relayed through Apple Health read as
one ring.

Four tables plus one nullable FK. All additive - no existing column is altered or
dropped, and data_source.device_id is ON DELETE SET NULL so removing a device can
never remove ingested data.

The backfill is deliberately conservative. It creates one device per distinct
(user_id, provider, device_model) among rows that actually carry a device_model,
and attributes only those rows. It does not group across providers, and it leaves
rows with no device_model unattributed. Rationale: a wrong split is visible and
reversible, while a wrong merge silently pools two units' samples into one stream
and may not be caught until it is in an analysis, by which point the samples cannot
be separated again. Unattributed is a normal resting state, not an error.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d3f1a8c2e5b4"
down_revision: Union[str, None] = "923694d3b177"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("brand", sa.String(length=64), nullable=True),
        sa.Column("model_raw", sa.String(length=100), nullable=True),
        sa.Column("model_display", sa.String(length=100), nullable=True),
        sa.Column("device_type", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("label_source", sa.String(length=32), nullable=False),
        sa.Column("wear_location", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_device_user_id", "device", ["user_id"])
    op.create_index("ix_device_user_brand_type", "device", ["user_id", "brand", "device_type"])

    op.create_table(
        "device_identity",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("route", sa.String(length=32), nullable=False),
        sa.Column("id_kind", sa.String(length=48), nullable=False),
        sa.Column("id_value", sa.String(length=255), nullable=False),
        sa.Column("confidence", sa.String(length=32), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_id"], ["device.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Scoped to the user, not global: these values are only unique within an account
    # (two users' Health Connect package names are identical). This index is the
    # lookup that makes detection idempotent.
    op.create_index(
        "uq_device_identity_claim",
        "device_identity",
        ["user_id", "route", "id_kind", "id_value"],
        unique=True,
    )
    op.create_index("ix_device_identity_device_id", "device_identity", ["device_id"])

    op.create_table(
        "device_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=True),
        sa.Column("data_source_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: a row describing a device that has since been merged
        # away is exactly the row you need to reconstruct what happened, so it has to
        # outlive its subject. The ids also survive in meta, where they carry no FK.
        sa.ForeignKeyConstraint(["device_id"], ["device.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["data_source_id"], ["data_source.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_device_history_user_created", "device_history", ["user_id", "created_at"])
    op.create_index("ix_device_history_device_created", "device_history", ["device_id", "created_at"])
    op.create_index("ix_device_history_data_source", "device_history", ["data_source_id"])

    op.create_table(
        "device_link_proposal",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("device_a_id", sa.UUID(), nullable=False),
        sa.Column("device_b_id", sa.UUID(), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_a_id"], ["device.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["device_b_id"], ["device.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Callers order the pair before writing, so (a,b) and (b,a) cannot both exist
        # and the unique index below is enough to stop a duplicate proposal.
        sa.CheckConstraint("device_a_id < device_b_id", name="ck_device_link_proposal_ordered"),
    )
    op.create_index(
        "uq_device_link_proposal_pair",
        "device_link_proposal",
        ["device_a_id", "device_b_id"],
        unique=True,
    )
    op.create_index("ix_device_link_proposal_user_status", "device_link_proposal", ["user_id", "status"])

    op.add_column("data_source", sa.Column("device_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_data_source_device_id",
        "data_source",
        "device",
        ["device_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_data_source_device_id", "data_source", ["device_id"])

    _backfill_devices()


def _backfill_devices() -> None:
    """Create one device per distinct (user_id, provider, device_model) and attribute it.

    Only rows that actually carry a device_model take part. Rows without one are left
    unattributed rather than guessed at: they are the providers that report no device
    (Whoop, Oura's data endpoints, Garmin's wellness summaries), so any grouping would
    be a guess that later looks like a finding.

    Grouping never crosses providers. The same physical ring seen through Oura's API and
    relayed through Apple Health has no identifier in common, so the two arrive as two
    devices here and merging them is an inference that belongs in a reviewed proposal,
    not in a migration. A wrong split is visible and reversible; a wrong merge silently
    pools two units' samples and may not be caught until it is in an analysis.

    The device id is derived from the group key with md5 rather than gen_random_uuid()
    so that every statement below can recompute it without a CTE round-trip, and so a
    re-run is idempotent. PostgreSQL casts md5's 32 hex characters straight to uuid.
    """
    conn = op.get_bind()

    # The group key, spelled once. provider is part of it and must stay part of every
    # join below - matching on model alone pulls an Oura ring's direct row and its
    # Apple Health row onto the same device, which is the merge this backfill exists
    # to avoid.
    device_id_expr = "md5(ds.user_id::text || '|' || ds.provider || '|' || ds.device_model)::uuid"

    # min() rather than an arbitrary pick so brand and type are deterministic across
    # reruns and replicas. device_type is copied from data_source, never re-inferred,
    # so this migration cannot change how anything is typed.
    conn.execute(
        sa.text(f"""
            INSERT INTO device (
                id, user_id, brand, model_raw, model_display, device_type,
                label, label_source, is_active, first_seen_at, last_seen_at,
                updated_at, created_at
            )
            SELECT
                {device_id_expr},
                ds.user_id,
                min(ds.original_source_name),
                ds.device_model,
                NULL,
                COALESCE(min(ds.device_type), 'unknown'),
                NULL,
                'auto',
                true,
                min(ds.created_at),
                max(ds.created_at),
                now(),
                now()
            FROM data_source ds
            WHERE ds.device_model IS NOT NULL
            GROUP BY ds.user_id, ds.provider, ds.device_model
            ON CONFLICT (id) DO NOTHING
        """)
    )

    conn.execute(
        sa.text(f"""
            UPDATE data_source ds
            SET device_id = {device_id_expr}
            WHERE ds.device_id IS NULL
              AND ds.device_model IS NOT NULL
        """)
    )

    # Seed a MODEL_STRING identity claim per device so backfilled devices look exactly
    # like detected ones and re-running detection finds them instead of creating
    # duplicates. WEAK confidence: a model string is shared by two identical units, so
    # it narrows the field but never identifies one.
    conn.execute(
        sa.text(f"""
            INSERT INTO device_identity (
                id, user_id, device_id, route, id_kind, id_value, confidence,
                first_seen_at, last_seen_at, created_at
            )
            SELECT
                gen_random_uuid(),
                ds.user_id,
                {device_id_expr},
                ds.provider,
                'model_string',
                ds.device_model,
                'weak',
                min(ds.created_at),
                max(ds.created_at),
                now()
            FROM data_source ds
            WHERE ds.device_model IS NOT NULL
            GROUP BY ds.user_id, ds.provider, ds.device_model
            ON CONFLICT (user_id, route, id_kind, id_value) DO NOTHING
        """)
    )

    # Seed the audit trail. Without this the first entry for a backfilled device would
    # be whatever a person changed later, with no record of where the attribution came
    # from in the first place.
    conn.execute(
        sa.text("""
            INSERT INTO device_history (
                id, user_id, device_id, data_source_id, action, actor, reason, meta, created_at
            )
            SELECT
                gen_random_uuid(),
                d.user_id,
                d.id,
                NULL,
                'detected',
                'system:migration d3f1a8c2e5b4',
                'Backfilled from existing data_source.device_model, grouped per (user, provider, device_model). '
                'Not grouped across providers: the same unit seen by two routes arrives here as two devices.',
                jsonb_build_object('model_raw', d.model_raw, 'device_type', d.device_type),
                now()
            FROM device d
        """)
    )


def downgrade() -> None:
    op.drop_index("ix_data_source_device_id", table_name="data_source")
    op.drop_constraint("fk_data_source_device_id", "data_source", type_="foreignkey")
    op.drop_column("data_source", "device_id")

    op.drop_index("ix_device_link_proposal_user_status", table_name="device_link_proposal")
    op.drop_index("uq_device_link_proposal_pair", table_name="device_link_proposal")
    op.drop_table("device_link_proposal")

    op.drop_index("ix_device_history_data_source", table_name="device_history")
    op.drop_index("ix_device_history_device_created", table_name="device_history")
    op.drop_index("ix_device_history_user_created", table_name="device_history")
    op.drop_table("device_history")

    op.drop_index("ix_device_identity_device_id", table_name="device_identity")
    op.drop_index("uq_device_identity_claim", table_name="device_identity")
    op.drop_table("device_identity")

    op.drop_index("ix_device_user_brand_type", table_name="device")
    op.drop_index("ix_device_user_id", table_name="device")
    op.drop_table("device")
