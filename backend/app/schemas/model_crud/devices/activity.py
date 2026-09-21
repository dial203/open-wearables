"""What a data source has reported lately, and which account it came through.

The registry says where a stream came from; this says what is in it. For a source
whose name identifies nothing - "Bluetooth Device", a bare bundle id - the shape of
the data is the only evidence of what the hardware is and where it is worn.

The account travels with it because the two questions are asked together: a
participant wearing two of one brand needs the classification and the login e-mail
beside the data, not behind a hover, before anyone decides which unit this is.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

# Clamped rather than free-form: the counts are grouped aggregates over the largest
# tables in the schema, and an unbounded window invites a request that scans all of
# them. 365 is past any "recently" this is meant to answer.
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 365
DEFAULT_WINDOW_DAYS = 30


class ActivityBucket(BaseModel):
    """One kind of thing a source reported, and how much of it."""

    label: str = Field(
        ...,
        description="Event category, score category, or time-series code, as stored.",
        examples=["sleep"],
    )
    count: int = Field(..., description="Rows in the window.", examples=[14])
    first_at: datetime = Field(..., description="Earliest row in the window.")
    last_at: datetime = Field(..., description="Most recent row in the window.")


class SourceActivityResponse(BaseModel):
    """A per-source preview: what arrived, when, and through which account."""

    data_source_id: UUID
    provider: str
    source: str | None = None
    device_model: str | None = None
    device_id: UUID | None = Field(
        None, description="The device this source is attributed to, or null when unattributed."
    )

    # Denormalised rather than looked up client-side. This endpoint exists to answer
    # "what is this thing", and the account is half that answer.
    user_connection_id: UUID | None = None
    account_label: str | None = Field(None, examples=["P01 arm A"])
    account_email: str | None = Field(None, examples=["p01.left@lab.example.edu"])
    account_type: str | None = Field(
        None,
        description="personal, validation, reliability, monitoring, testing, other. Null when unclassified.",
        examples=["validation"],
    )

    events: list[ActivityBucket] = Field(
        default_factory=list,
        description="Sleep sessions, workouts, naps - by the category the provider gave.",
    )
    scores: list[ActivityBucket] = Field(
        default_factory=list,
        description="Readiness, recovery, strain, sleep score - whatever the provider scores.",
    )
    metrics: list[ActivityBucket] = Field(
        default_factory=list,
        description="Time-series codes, most rows first. What separates a strap from a ring.",
    )
    last_seen_at: datetime | None = Field(
        None,
        description=(
            "Most recent row of any kind in the window, or null when the source reported "
            "nothing in it. Null is informative: a source that has gone quiet is a device "
            "that came off, or a pairing that broke."
        ),
    )


class SourceActivityListResponse(BaseModel):
    items: list[SourceActivityResponse]
    total: int
    window_days: int = Field(..., description="How far back the counts reach.", examples=[30])
