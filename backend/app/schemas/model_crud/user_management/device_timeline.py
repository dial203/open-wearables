"""An account's dated device history, and the re-file that brings stored data in line.

See app/utils/device_timeline.py for what a timeline means and
app/services/device_timeline_service.py for what a re-file moves.
"""

from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class DevicePeriodInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_label: str = Field(..., max_length=100, description='The device worn in this stretch, e.g. "Venu X1"')
    effective_from: AwareDatetime | None = Field(
        None,
        description=(
            "When this device started, with its UTC offset. Null means from the start of the account's data; "
            "at most one period may say so. Each period runs until the next one's start."
        ),
    )

    @field_validator("device_label")
    @classmethod
    def _label_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("device_label must not be blank")
        return stripped


class DeviceTimelineUpdate(BaseModel):
    """The account's whole timeline. Replaces what is there; an empty list removes it."""

    model_config = ConfigDict(extra="forbid")

    periods: list[DevicePeriodInput] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def _starts_distinct(self) -> "DeviceTimelineUpdate":
        starts = [p.effective_from for p in self.periods]
        if starts.count(None) > 1:
            raise ValueError("only one period may have no start (effective_from null)")
        dated = [s for s in starts if s is not None]
        if len(set(dated)) != len(dated):
            raise ValueError("two periods cannot start at the same instant")
        return self


class DevicePeriodRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    device_label: str
    effective_from: datetime | None
    # Exclusive end: the next period's start, or null for the current one.
    effective_to: datetime | None = None
    created_at: datetime


class DeviceTimelineRead(BaseModel):
    connection_id: UUID
    provider: str
    # The current period's label - kept equal to the account's device_label.
    device_label: str | None
    periods: list[DevicePeriodRead]


class DeviceRefileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = Field(True, description="Report what would move without moving anything")
    include_data_source_ids: list[UUID] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "Sources whose device name matches a timeline label but whose origin OW did not record (they "
            "predate it). Name one only if the provider never reports a device on this account: a model the "
            "provider stamped must not be re-filed by a stated timeline."
        ),
    )


class RefileSource(BaseModel):
    """One of the account's data sources, and whether a re-file may move rows off it."""

    data_source_id: UUID
    device_model: str | None
    device_model_origin: str | None
    source: str | None
    eligible: bool
    reason: str


class RefileCounts(BaseModel):
    event_records: int = 0
    samples: int = 0
    archive_days: int = 0
    health_scores: int = 0


class RefileMove(BaseModel):
    """Rows on one source that the timeline assigns to another label, for one period."""

    from_data_source_id: UUID
    from_device_model: str | None
    to_device_model: str
    # Null in a dry run when the destination does not exist yet.
    to_data_source_id: UUID | None
    period_start: datetime | None
    period_end: datetime | None
    moved: RefileCounts
    # Left where they are: the destination already holds a row at the same instant.
    conflicts: RefileCounts
    # Archived days this period's boundary falls inside - one daily row cannot be split.
    archive_days_straddling: int = 0


class DeviceRefileResult(BaseModel):
    connection_id: UUID
    dry_run: bool
    sources: list[RefileSource]
    moves: list[RefileMove]
    # Rows on an eligible source that no period covers: no statement, so not moved.
    uncovered: RefileCounts
    total_moved: RefileCounts
    total_conflicts: RefileCounts
