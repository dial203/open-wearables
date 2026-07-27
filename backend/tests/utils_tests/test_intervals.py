"""Interval-union maths behind the sleep time-in-bed cap (pure, no DB)."""

from datetime import datetime, timedelta, timezone

from app.utils.intervals import merged_span_minutes

UTC = timezone.utc


def _at(hour: float) -> datetime:
    return datetime(2026, 7, 25, tzinfo=UTC) + timedelta(hours=hour)


def test_empty_and_degenerate_inputs() -> None:
    assert merged_span_minutes([]) == 0
    assert merged_span_minutes([(_at(1), _at(1))]) == 0  # zero length
    assert merged_span_minutes([(_at(5), _at(2))]) == 0  # inverted


def test_single_interval() -> None:
    assert merged_span_minutes([(_at(0), _at(7))]) == 420


def test_disjoint_intervals_sum_and_exclude_the_gap() -> None:
    # 2 h + 1 h with a 3 h gap between them -> 180, not the 6 h span
    assert merged_span_minutes([(_at(0), _at(2)), (_at(5), _at(6))]) == 180


def test_overlapping_intervals_counted_once() -> None:
    # 0-6 and 4-8 overlap by 2 h -> union is 8 h, naive sum would be 10 h
    assert merged_span_minutes([(_at(0), _at(6)), (_at(4), _at(8))]) == 480


def test_fully_contained_interval_adds_nothing() -> None:
    # the real Whoop case: 04:28-11:18 sits entirely inside 02:10-11:53
    outer = (datetime(2026, 7, 25, 2, 10, tzinfo=UTC), datetime(2026, 7, 25, 11, 53, tzinfo=UTC))
    inner = (datetime(2026, 7, 25, 4, 28, tzinfo=UTC), datetime(2026, 7, 25, 11, 18, tzinfo=UTC))
    union = merged_span_minutes([outer, inner])
    assert union == 583  # the outer window alone
    # naive summing would report ~992 min for a 583 min night
    assert union < 583 + 409


def test_input_order_does_not_matter() -> None:
    a, b, c = (_at(4), _at(8)), (_at(0), _at(6)), (_at(7), _at(9))
    assert merged_span_minutes([a, b, c]) == merged_span_minutes([c, b, a]) == 540


def test_adjacent_intervals_touching_exactly_merge() -> None:
    assert merged_span_minutes([(_at(0), _at(3)), (_at(3), _at(5))]) == 300


def test_real_oura_night_with_four_overlapping_records() -> None:
    """Four records spanning 02:09-18:34 that naively summed to 38.8 h."""
    records = [
        (datetime(2026, 7, 25, 2, 9, 59, tzinfo=UTC), datetime(2026, 7, 25, 12, 0, 31, tzinfo=UTC)),
        (datetime(2026, 7, 25, 3, 34, 17, tzinfo=UTC), datetime(2026, 7, 25, 18, 34, 17, tzinfo=UTC)),
        (datetime(2026, 7, 25, 4, 25, 30, tzinfo=UTC), datetime(2026, 7, 25, 11, 22, 0, tzinfo=UTC)),
        (datetime(2026, 7, 25, 4, 26, 3, tzinfo=UTC), datetime(2026, 7, 25, 11, 26, 58, tzinfo=UTC)),
    ]
    union = merged_span_minutes(records)
    # they all overlap, so the union is the full 02:09:59 -> 18:34:17 window
    assert union == 984  # 16.4 h
    naive_sum = 590 + 900 + 416 + 420  # 38.8 h
    assert union < naive_sum
