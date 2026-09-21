from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.services import DeveloperDep

router = APIRouter()


class ConfigResponse(BaseModel):
    """Instance feature flags for the admin panel. Additive-only: append new flags,
    never remove or repurpose, so the frontend stays backward compatible."""

    outgoing_webhooks_enabled: bool
    # Whether reads leave out an aggregator's copy of a maker that is also connected
    # directly. False means every source is returned, as before the rule existed.
    relay_dedup_enabled: bool


@router.get("/config", response_model=ConfigResponse)
def get_config(_developer: DeveloperDep):
    return ConfigResponse(
        outgoing_webhooks_enabled=settings.outgoing_webhooks_enabled,
        relay_dedup_enabled=settings.relay_dedup_enabled,
    )
