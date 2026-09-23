"""provider_metadata passthrough on samples and events

Revision ID: b4e8f2a7c915
Revises: e1b7d4a9c3f5

Keep the per-record metadata the platforms send and ingest has been discarding.

`MetricRecord.metadata`, `SleepRecord.metadata` and `Workout.metadata` have been on
the mobile SDK schema since it was written, parsed by Pydantic on every sync and then
read by nothing. Grepping the backend for a consumer, or for any `HKMetadataKey`,
returns zero hits.

What that costs is specific. HealthKit attaches the provenance of a heart-rate sample
to the sample: which Bluetooth peripheral produced it, and where on the body it was
measured. The iOS SDK forwards it (`_mapQuantityEfficient` passes `q.metadata`
through), the payload arrives with it, and it is dropped here. For data written by an
aggregating app - Apple's own Fitness app collects workouts recorded by a watch, by
AirPods, or by both at once - that metadata is the only thing distinguishing which
sensor produced a given record, because every one of them reports the same writer and
the same relaying handset. Without it those samples have no identifiable comparator,
which is a validity problem for any agreement analysis drawn from them, not only an
attribution one.

Stored raw and uninterpreted, deliberately. The keys differ per platform and per
writer, the iOS SDK stringifies values on the way out (an `HKQuantity` arrives as
"70 count/min"), and promoting any particular key to a typed column is a decision that
needs the real distribution of keys in front of it. This migration exists so that
distribution can be observed at all; normalising it comes after, from stored data
rather than from a capture pipeline.

Nullable, additive, and written only when a record carries a non-empty dict, so rows
whose platform sends nothing stay NULL and cost a null-bitmap bit. On `data_point_series`
this is the platform's largest table, and Apple heart-rate samples do commonly carry
metadata, so expect real growth there - on the order of a hundred bytes per sample that
has any. That is the price of the data being recoverable at all; it is not recoverable
retroactively, because nothing kept it.

No backfill. Every record already ingested lost its metadata at the SDK boundary and
cannot get it back from the database. Re-reading it means a fresh export from the
source platform - on iOS, `resetAnchors()` and a full re-sync.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b4e8f2a7c915"
down_revision: Union[str, None] = "e1b7d4a9c3f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "data_point_series",
        sa.Column("provider_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "event_record",
        sa.Column("provider_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("event_record", "provider_metadata")
    op.drop_column("data_point_series", "provider_metadata")
