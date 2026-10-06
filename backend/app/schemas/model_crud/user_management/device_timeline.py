"""An account's dated device history, and the re-file that brings stored data in line.

See app/utils/device_timeline.py for what a timeline means,
app/utils/device_switch_detection.py for how one is detected, and
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
    auto: bool | None = Field(
        None,
        description=(
            "Whether new device switches seen in the provider's workouts are added automatically. Omit to leave as is."
        ),
    )

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
    # "detected" from the provider's workouts, or "stated" by a person.
    origin: str
    created_at: datetime
    # The previous device's last workout before this period started: the switch fell
    # between it and effective_from. Null when no workout says (first period, or none seen).
    previous_last_seen: datetime | None = None


class DetectedPeriodRead(BaseModel):
    device_label: str
    effective_from: datetime | None
    evidence_count: int
    first_seen: datetime
    last_seen: datetime
    previous_last_seen: datetime | None


class DeviceSightingRead(BaseModel):
    device_label: str
    evidence_count: int
    first_seen: datetime
    last_seen: datetime


class DeviceDetectionRead(BaseModel):
    """What the provider's own device names say, beside what the account holds."""

    # The provider names devices on workouts (Garmin, Polar, Suunto, Fitbit).
    supported: bool
    # The history the account's workouts show, from the start, ignoring hand edits.
    proposed: list[DetectedPeriodRead]
    # Each device the workouts name, in the order first seen.
    devices: list[DeviceSightingRead]
    # The saved history differs from ``proposed`` (labels or start instants).
    differs: bool


class DeviceTimelineRead(BaseModel):
    connection_id: UUID
    provider: str
    # The current period's label - kept equal to the account's device_label.
    device_label: str | None
    periods: list[DevicePeriodRead]
    # New switches in the provider's workouts are added automatically.
    auto: bool
    # Detection only adds switches after this; null means it owns the whole history.
    detect_from: datetime | None
    detection: DeviceDetectionRead


class DeviceDetectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reset: bool = Field(
        False,
        description=(
            "Rebuild the whole history from the provider's workouts, discarding periods a person "
            "stated. Without it only switches after the last hand edit are added."
        ),
    )
    refile: bool = Field(True, description="Move stored records to match the new history (label-filled rows only)")


class DeviceRefileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = Field(True, description="Report what would move without moving anything")
    include_data_source_ids: list[UUID] = Field(
        default_factory=list,
        max_length=200,
        description=(
            "Sources whose device-name origin OW did not record (they predate it). Name one only if its "
            "device name is a label someone typed, not one the provider sent (Garmin names the watch on "
            "activities, never on sleep): a model the provider stamped must not be re-filed by a stated timeline."
        ),
    )


class RefileSource(BaseModel):
    """One of the account's data sources, and whether a re-file may move rows off it."""

    data_source_id: UUID
    device_model: str | None
    device_model_origin: str | None
    source: str | None
    eligible: bool
    # Its workouts, samples inside them and archived days stay: on a provider that
    # names the device on workouts and nothing else, those are its captures.
    captures_kept: bool = False
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
    # Archived days left on a source whose captures are kept (RefileSource.captures_kept).
    archive_days_kept: int = 0


class DeviceRefileResult(BaseModel):
    connection_id: UUID
    dry_run: bool
    sources: list[RefileSource]
    moves: list[RefileMove]
    # Rows on an eligible source that no period covers: no statement, so not moved.
    uncovered: RefileCounts
    total_moved: RefileCounts
    total_conflicts: RefileCounts


class DeviceDetectResult(BaseModel):
    changed: bool
    timeline: DeviceTimelineRead
    # The re-file that followed a change; null when nothing changed or none was asked for.
    refile: DeviceRefileResult | None = None
