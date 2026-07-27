"""Interval helpers for collapsing overlapping time ranges."""

from datetime import datetime


def merged_span_minutes(intervals: list[tuple[datetime, datetime]]) -> int:
    """Total minutes covered by the union of the given intervals.

    Overlapping ranges are counted once and gaps between ranges are excluded, so a
    night made of several overlapping records reports the time actually covered
    rather than the sum of the parts. Zero-length and inverted ranges are ignored.

    Providers regularly re-issue a night under a new id (a revised Whoop sleep, the
    same Oura night seen under two ring models), which leaves several heavily
    overlapping records for one night. Summing their durations double-counts the
    overlap - four such records once reported 38.8 h of sleep across a 16.4 h window.
    """
    valid = [(start, end) for start, end in intervals if start is not None and end is not None and end > start]
    if not valid:
        return 0

    total = 0.0
    current_start, current_end = min(valid)  # earliest start; ties break on end
    for start, end in sorted(valid)[1:]:
        if start > current_end:  # disjoint - bank the run and start a new one
            total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
        elif end > current_end:  # overlaps and extends the current run
            current_end = end
    total += (current_end - current_start).total_seconds()

    return int(total // 60)
