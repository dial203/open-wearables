"""Tests that MCP tool handlers translate typed client errors into the
documented error-envelope shape instead of bubbling the exception up.

Each tool has two error paths worth covering:
- inner `NotFoundError` from the user-lookup block -> "User not found" envelope
- outer `OpenWearablesError` from the downstream resource fetch -> generic error envelope
"""

import pytest
from fastmcp.tools import FunctionTool
from pytest_httpx import HTTPXMock

from app.tools.activity import get_activity_summary
from app.tools.sleep import get_sleep_summary
from app.tools.timeseries import get_timeseries
from app.tools.users import get_users
from app.tools.workouts import get_workout_events

USER_ID = "00000000-0000-0000-0000-000000000000"
USER_PAYLOAD = {
    "id": USER_ID,
    "first_name": "Test",
    "last_name": "User",
    "email": "test@example.com",
}


async def test_get_users_returns_empty_envelope_on_auth_error(httpx_mock: HTTPXMock) -> None:
    """`get_users` translates a backend 401 into the documented empty-envelope shape."""
    httpx_mock.add_response(
        method="GET",
        url="https://api.test.com/api/v1/users?limit=10",
        status_code=401,
    )

    result = await get_users.fn()

    assert result["users"] == []
    assert result["total"] == 0
    assert "error" in result


@pytest.mark.parametrize(
    "tool",
    [
        pytest.param(get_activity_summary, id="activity"),
        pytest.param(get_sleep_summary, id="sleep"),
        pytest.param(get_workout_events, id="workouts"),
    ],
)
async def test_summary_tools_return_user_not_found_envelope_on_404(
    tool: FunctionTool,
    httpx_mock: HTTPXMock,
) -> None:
    """Summary tools turn a 404 on user lookup into the 'User not found' envelope (inner except block)."""
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.test.com/api/v1/users/{USER_ID}",
        status_code=404,
    )

    result = await tool.fn(
        user_id=USER_ID,
        start_date="2026-01-01",
        end_date="2026-01-07",
    )

    assert result["error"] == f"User not found: {USER_ID}"
    assert "details" in result


async def test_get_timeseries_returns_user_not_found_envelope_on_404(httpx_mock: HTTPXMock) -> None:
    """`get_timeseries` turns a 404 on user lookup into the 'User not found' envelope."""
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.test.com/api/v1/users/{USER_ID}",
        status_code=404,
    )

    result = await get_timeseries.fn(
        user_id=USER_ID,
        start_time="2026-04-05T00:00:00Z",
        end_time="2026-04-05T23:59:59Z",
        types=["heart_rate"],
    )

    assert result["error"] == f"User not found: {USER_ID}"
    assert "details" in result


async def test_get_activity_summary_returns_generic_error_envelope_on_downstream_401(
    httpx_mock: HTTPXMock,
) -> None:
    """Downstream 401 (after user lookup succeeds) surfaces via the generic error envelope, not 'User not found'."""
    httpx_mock.add_response(
        method="GET",
        url=f"https://api.test.com/api/v1/users/{USER_ID}",
        json=USER_PAYLOAD,
    )
    httpx_mock.add_response(
        method="GET",
        url=(
            f"https://api.test.com/api/v1/users/{USER_ID}/summaries/activity"
            "?start_date=2026-01-01&end_date=2026-01-07&limit=100&sort_order=asc"
        ),
        status_code=401,
    )

    result = await get_activity_summary.fn(
        user_id=USER_ID,
        start_date="2026-01-01",
        end_date="2026-01-07",
    )

    assert "error" in result
    assert "Invalid API key" in result["error"]
    assert not result["error"].startswith("User not found")


# ---------------------------------------------------------------------------
# Cursor pagination
#
# The summary tools used to read page one and stop. Because the API returns
# records oldest-first, a range longer than one page came back as its oldest
# slice with the remainder silently dropped — no error, no flag. These lock in
# that every page is now walked.
# ---------------------------------------------------------------------------


def _page(records: list[dict], next_cursor: str | None) -> dict:
    return {
        "data": records,
        "pagination": {"next_cursor": next_cursor, "previous_cursor": None, "has_more": bool(next_cursor)},
    }


async def test_get_activity_summary_walks_every_page(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"https://api.test.com/api/v1/users/{USER_ID}", json=USER_PAYLOAD)
    httpx_mock.add_response(json=_page([{"date": "2026-01-01", "steps": 100}], "CURSOR_2"))
    httpx_mock.add_response(json=_page([{"date": "2026-04-01", "steps": 900}], None))

    result = await get_activity_summary.fn(user_id=USER_ID, start_date="2026-01-01", end_date="2026-04-01")

    assert [r["date"] for r in result["records"]] == ["2026-01-01", "2026-04-01"]
    assert result["summary"]["total_steps"] == 1000
    assert result["truncated"] is False
    # The second request must carry the cursor the first page handed back.
    assert "cursor=CURSOR_2" in str(httpx_mock.get_requests()[-1].url)


async def test_get_sleep_summary_walks_every_page(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"https://api.test.com/api/v1/users/{USER_ID}", json=USER_PAYLOAD)
    httpx_mock.add_response(json=_page([{"date": "2026-01-01"}], "CURSOR_2"))
    httpx_mock.add_response(json=_page([{"date": "2026-04-01"}], None))

    result = await get_sleep_summary.fn(user_id=USER_ID, start_date="2026-01-01", end_date="2026-04-01")

    assert len(result["records"]) == 2
    assert result["truncated"] is False
    assert "cursor=CURSOR_2" in str(httpx_mock.get_requests()[-1].url)


async def test_get_workout_events_walks_every_page(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"https://api.test.com/api/v1/users/{USER_ID}", json=USER_PAYLOAD)
    httpx_mock.add_response(json=_page([{"id": "w1", "type": "running"}], "CURSOR_2"))
    httpx_mock.add_response(json=_page([{"id": "w2", "type": "cycling"}], None))

    result = await get_workout_events.fn(user_id=USER_ID, start_date="2026-01-01", end_date="2026-04-01")

    assert result["summary"]["total_workouts"] == 2
    assert result["truncated"] is False
    assert "cursor=CURSOR_2" in str(httpx_mock.get_requests()[-1].url)


async def test_pagination_that_never_ends_is_reported_not_hidden(
    httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hitting the page ceiling must surface as truncated=True, never as a short answer."""
    monkeypatch.setattr("app.tools.activity._MAX_PAGES", 3)
    httpx_mock.add_response(method="GET", url=f"https://api.test.com/api/v1/users/{USER_ID}", json=USER_PAYLOAD)
    for _ in range(3):
        httpx_mock.add_response(json=_page([{"date": "2026-01-01", "steps": 1}], "ALWAYS_MORE"))

    result = await get_activity_summary.fn(user_id=USER_ID, start_date="2026-01-01", end_date="2026-12-31")

    assert result["truncated"] is True
    assert len(result["records"]) == 3


async def test_activity_sort_order_reaches_the_api(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="GET", url=f"https://api.test.com/api/v1/users/{USER_ID}", json=USER_PAYLOAD)
    httpx_mock.add_response(json=_page([{"date": "2026-04-01", "steps": 900}], None))

    await get_activity_summary.fn(user_id=USER_ID, start_date="2026-01-01", end_date="2026-04-01", sort_order="desc")

    assert "sort_order=desc" in str(httpx_mock.get_requests()[-1].url)
