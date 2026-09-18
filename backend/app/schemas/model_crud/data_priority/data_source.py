from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.enums import IngestionRoute, ProviderName


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

    model_config = {"from_attributes": True}


class DataSourceListResponse(BaseModel):
    items: list[DataSourceResponse]
    total: int
