from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.enums import IngestionRoute, ProviderName, RelayVisibility


class DataSourceBase(BaseModel):
    user_id: UUID
    provider: ProviderName
    user_connection_id: UUID | None = None
    device_model: str | None = None
    software_version: str | None = None
    source: str | None = None
    device_type: str | None = None
    original_source_name: str | None = None


class DataSourceCreate(DataSourceBase):
    id: UUID


class DataSourceUpdate(BaseModel):
    provider: ProviderName | None = None
    user_connection_id: UUID | None = None
    device_model: str | None = None
    software_version: str | None = None
    source: str | None = None
    device_type: str | None = None
    original_source_name: str | None = None


class DataSourceResponse(BaseModel):
    id: UUID
    user_id: UUID
    provider: ProviderName
    user_connection_id: UUID | None = None
    device_model: str | None = None
    software_version: str | None = None
    source: str | None = None
    device_type: str | None = None
    original_source_name: str | None = None
    display_name: str | None = None
    device_id: UUID | None = Field(
        None,
        description=(
            "The physical device this source is attributed to, or `null` when it is "
            "unattributed. Unattributed is a normal resting state: detection only groups "
            "what a provider itself identifies."
        ),
    )
    attribution_locked_at: datetime | None = Field(
        None,
        description=(
            "When someone detached this source from a device by hand. While set, detection "
            "leaves it unattributed instead of re-attaching it on the next sync. Linking it "
            "to a device clears it."
        ),
    )
    # Denormalised from the connection so a data-source listing is readable on
    # its own. A participant with two Garmins has two rows here that are
    # otherwise identical down to the device model, and the account is the only
    # thing that tells them apart.
    account_type: str | None = Field(
        None,
        description=(
            "What the connected account is for: personal, validation, reliability, "
            "monitoring, testing, other. Null when nobody has classified it."
        ),
        example="validation",
    )
    account_label: str | None = Field(
        None,
        description="Name of the connected account this source arrived through.",
        example="P01 arm A",
    )
    account_email: str | None = Field(
        None,
        description="Login e-mail of the connected account this source arrived through.",
        example="p01.left@lab.example.edu",
    )
    ingestion_route: IngestionRoute = Field(
        IngestionRoute.DIRECT,
        description=(
            "Whether this source came straight from the maker's API (`direct`) or was "
            "relayed through an aggregator platform such as Apple Health or Google Health "
            "(`aggregator`). When `aggregator`, `provider` names the platform and "
            "`original_source_name` names the brand that actually recorded the data."
        ),
    )
    relay_visibility: RelayVisibility = Field(
        RelayVisibility.AUTO,
        description=(
            "Override of the redundant-relay rule for this source: `auto` follows it, "
            "`always` keeps the source visible whatever the rule says, `never` hides it "
            "outright. Set with PATCH /users/{user_id}/data-sources/{data_source_id}."
        ),
    )
    redundant_relay: bool = Field(
        False,
        description=(
            "True when this source relays a maker that is also connected directly, so reads "
            "leave it out for the span the direct route covers. It is a classification, not a "
            "statement that the source is hidden everywhere: the span is resolved per read, and "
            "`include_redundant_relays=true` returns the data either way."
        ),
    )
    direct_provider: str | None = Field(
        None,
        description="The provider delivering this brand directly, when one does.",
        example="oura",
    )

    model_config = {"from_attributes": True}


class DataSourceRelayUpdate(BaseModel):
    """Set (or clear) a person's override of the redundant-relay rule for one source."""

    relay_visibility: RelayVisibility = Field(
        description=(
            "`auto` to follow the rule, `always` to keep this source in every read, `never` to "
            "hide it outright. Nothing is deleted at any setting."
        ),
    )


class DataSourceListResponse(BaseModel):
    items: list[DataSourceResponse]
    total: int
