"""Request and response shapes for the device registry API."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.enums import DeviceType, LabelSource


class DeviceIdentityResponse(BaseModel):
    """One route's identity claim about a device."""

    route: str = Field(..., description="Ingest route that issued the claim.", examples=["oura"])
    id_kind: str = Field(..., examples=["oura_config_id"])
    id_value: str
    confidence: str = Field(
        ...,
        description=(
            "`strong` if the provider issued it and it distinguishes two same-model units; "
            "`weak` if it names the writing app or the hardware model, which identical units share."
        ),
        examples=["strong"],
    )
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None

    model_config = {"from_attributes": True}


class DeviceDataSourceResponse(BaseModel):
    """A data source attributed to a device, kept whole rather than merged into it."""

    id: UUID
    provider: str
    source: str | None = None
    device_model: str | None = None
    device_type: str | None = None
    original_source_name: str | None = None

    model_config = {"from_attributes": True}


class DeviceResponse(BaseModel):
    id: UUID
    user_id: UUID
    brand: str | None = None
    model_raw: str | None = Field(
        None,
        description="The provider's own model string, verbatim. Never normalized, never editable.",
    )
    model_display: str | None = Field(None, description="Marketing name for model_raw, display only.")
    device_type: DeviceType | str
    label: str | None = None
    label_source: LabelSource | str = Field(
        ...,
        description="`manual` once a person sets a label; detection never overwrites one.",
    )
    wear_location: str | None = None
    notes: str | None = None
    is_active: bool
    retired_at: datetime | None = Field(
        None,
        description=(
            "When this unit stopped being worn. Set it on a replacement: an identical "
            "replacement unit reports the same brand, model and routes, so without this "
            "date pre- and post-swap samples pool into one device."
        ),
    )
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    identities: list[DeviceIdentityResponse] = []
    data_sources: list[DeviceDataSourceResponse] = []

    model_config = {"from_attributes": True}


class DeviceListResponse(BaseModel):
    items: list[DeviceResponse]
    total: int


class DeviceCreate(BaseModel):
    """Create a device by hand, for hardware no provider has reported yet."""

    device_type: DeviceType = DeviceType.UNKNOWN
    brand: str | None = Field(None, max_length=64)
    model_raw: str | None = Field(None, max_length=100)
    label: str | None = Field(None, max_length=100)
    wear_location: str | None = Field(None, max_length=32)
    notes: str | None = None
    reason: str | None = Field(None, description="Recorded in the device's history.")


class DeviceUpdate(BaseModel):
    """Hand edits. Only fields that are our interpretation, never the provider's report.

    `brand` and `model_raw` are absent on purpose: they record what the provider
    claimed the hardware was, and editing them in place would erase the only evidence
    of that. Correct the presentation with `model_display` instead.
    """

    label: str | None = Field(None, max_length=100)
    device_type: DeviceType | None = None
    wear_location: str | None = Field(None, max_length=32)
    notes: str | None = None
    model_display: str | None = Field(None, max_length=100)
    reason: str | None = Field(None, description="Recorded in the device's history.")


class DeviceRetireRequest(BaseModel):
    retired: bool = True
    effective_at: datetime | None = Field(
        None,
        description="When the unit stopped being worn. Defaults to now. Set it explicitly for a past swap.",
    )
    reason: str | None = None


class DeviceLinkRequest(BaseModel):
    """Attribute a data source to this device, or move it here from another one."""

    data_source_id: UUID
    reason: str | None = None


class DeviceMergeRequest(BaseModel):
    """Fold another device into this one.

    Irreversible: the absorbed row is deleted. Everything needed to rebuild it by
    hand is written to history first. The data sources are re-pointed, never
    combined - direct and relayed data differ in freshness and rounding, so the
    comparison survives the merge.
    """

    absorb_device_id: UUID
    reason: str | None = None


class DeviceSplitRequest(BaseModel):
    """Move some of this device's data sources onto a new one.

    The undo for a merge, and the fix for detection that grouped too eagerly.
    Splitting every source off is refused - that is a rename.
    """

    data_source_ids: list[UUID] = Field(..., min_length=1)
    reason: str | None = None


class DeviceHistoryResponse(BaseModel):
    id: UUID
    device_id: UUID | None = None
    data_source_id: UUID | None = None
    action: str
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    actor: str | None = None
    reason: str | None = None
    meta: dict[str, Any] | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class DeviceHistoryListResponse(BaseModel):
    items: list[DeviceHistoryResponse]
    total: int


class LinkProposalResponse(BaseModel):
    """A suggestion that two devices are one unit seen by different routes.

    Never applied automatically. `evidence` carries what produced the score so a
    reviewer can judge the proposal rather than trust it.
    """

    id: UUID
    device_a_id: UUID
    device_b_id: UUID
    score: Decimal
    evidence: dict[str, Any] | None = None
    status: str
    decided_at: datetime | None = None
    decided_by: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class LinkProposalListResponse(BaseModel):
    items: list[LinkProposalResponse]
    total: int


class LinkProposalDecision(BaseModel):
    accepted: bool = Field(
        ...,
        description=(
            "`true` merges device_b into device_a. `false` records the rejection "
            "permanently, so the pair is never proposed again."
        ),
    )
    reason: str | None = None
