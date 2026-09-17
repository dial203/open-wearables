"""Pin the `filter_by_priority` defaults that consumers rely on.

The fork collapses summaries to the highest-priority source per day by default;
upstream leaves `/timeseries` and `/events/sleep` returning every source. The split is
deliberate, and the service-level tests all pass the flag explicitly, so nothing else
would notice an upstream merge flipping an endpoint default - and a flip is silent:
callers get more rows per day, or fewer, with no error.

Two consumers depend on the collapsed default: the MCP tools are written around one
summary record per day, and the frontend passes `filter_by_priority: false` only where
it wants per-source rows. Change these numbers only together with those callers.
"""

import pytest

from app.main import api

COLLAPSED_BY_DEFAULT = (
    "/api/v1/users/{user_id}/summaries/activity",
    "/api/v1/users/{user_id}/summaries/sleep",
    "/api/v1/users/{user_id}/summaries/recovery",
)

EVERY_SOURCE_BY_DEFAULT = (
    "/api/v1/users/{user_id}/timeseries",
    "/api/v1/users/{user_id}/events/sleep",
)


def _default(schema: dict, path: str) -> bool:
    operation = schema["paths"][path]["get"]
    param = next(p for p in operation["parameters"] if p["name"] == "filter_by_priority")
    return param["schema"]["default"]


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    return api.openapi()


@pytest.mark.parametrize("path", COLLAPSED_BY_DEFAULT)
def test_summary_endpoints_collapse_to_priority_source_by_default(openapi_schema: dict, path: str) -> None:
    assert _default(openapi_schema, path) is True


@pytest.mark.parametrize("path", EVERY_SOURCE_BY_DEFAULT)
def test_timeseries_and_sleep_events_return_every_source_by_default(openapi_schema: dict, path: str) -> None:
    assert _default(openapi_schema, path) is False
