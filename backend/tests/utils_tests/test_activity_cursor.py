"""The activity cursor carries the account, and still reads cursors issued without it."""

from datetime import date

from app.utils.pagination import _encode_cursor_fields, decode_activity_cursor, encode_activity_cursor


def test_round_trips_the_account() -> None:
    cursor = encode_activity_cursor(date(2025, 12, 26), "garmin", None, "next", account_id="acct-1")
    assert decode_activity_cursor(cursor) == (date(2025, 12, 26), "garmin", None, "acct-1", "next")


def test_a_row_without_an_account_decodes_to_none() -> None:
    cursor = encode_activity_cursor(date(2025, 12, 26), "garmin", "fenix 7", "prev")
    assert decode_activity_cursor(cursor) == (date(2025, 12, 26), "garmin", "fenix 7", None, "prev")


def test_a_cursor_issued_before_the_account_was_in_the_key_still_decodes() -> None:
    # A client paginating across the upgrade holds one of these.
    legacy = _encode_cursor_fields(["2025-12-26", "garmin", "fenix 7"], "next")
    assert decode_activity_cursor(legacy) == (date(2025, 12, 26), "garmin", "fenix 7", None, "next")
