"""Sleep-onset latency and WASO read off a session's stage intervals.

Many sources publish a TOTAL awake figure and no WASO or latency. The two
differ by the latency, so a total cannot stand in for WASO. Where a session
carries its stage intervals, both follow from when each wake interval happened.
This is a derivation, not a value the source reported, so it is served under
its own name (``SleepSession.derived_from_stages``) and never written into the
source's own fields.

Rules:

- ``in_bed`` is a window, not a scoring, and is ignored. HealthKit publishes it
  beside the staged runs and it spans the whole session.
- ONSET is the first interval scored as sleep (``sleeping``, ``light``,
  ``deep``, ``rem``), however short. The length of the unbroken sleep that
  starts there is returned too, so a caller can spot a stray early epoch.
- LATENCY runs from the session start to onset. It is ``None`` unless the time
  before onset is covered by ``awake`` intervals: an uncovered or ``unknown``
  run-up says nothing about what happened, and a gap is not latency.
- WASO is every ``awake`` minute after onset, through the end of the last
  interval. The part after the final sleep interval is also returned on its own
  (``terminal_wake_minutes``), so onset-to-final-awakening is a subtraction.
- UNSCORED time after onset (``unknown`` intervals and uncovered gaps) is never
  counted as wake. It is returned as ``unaccounted_after_onset_minutes``.
- Nothing is derived from a session with no sleep interval.

Latency is measured from the session's own start. For a source whose session
starts when the user lies down (e.g. a Muse S session started at lights-out)
that is sleep-onset latency proper; for a source whose session is a window it
detected itself, it is latency from that window.
"""

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, Field

from app.constants.sleep import SleepStageType
from app.schemas.model_crud.activities.sleep import SleepStage

# Up to this much of the run-up may be unaccounted for and still be latency:
# sources round interval boundaries, and a few seconds is arithmetic, not a hole.
GAP_TOLERANCE_SECONDS = 60

# A first sleep run shorter than this is flagged as a possible stray epoch.
SHORT_FIRST_SLEEP_RUN_MINUTES = 10

_SLEEP = {SleepStageType.SLEEPING, SleepStageType.LIGHT, SleepStageType.DEEP, SleepStageType.REM}

Span = tuple[float, float]


class SleepOnsetMetrics(BaseModel):
    """Latency and WASO derived from stage intervals. See ``app.algorithms.sleep_onset``."""

    onset_time: datetime = Field(..., description="Start of the first interval scored as sleep")
    latency_minutes: float | None = Field(
        None,
        description=(
            "Session start to onset. Null unless the run-up is scored as awake. Measured from "
            "the session's own start, which for most sources is a window the device detected."
        ),
    )
    waso_minutes: float = Field(..., description="Awake minutes after onset, through the end of the session")
    terminal_wake_minutes: float = Field(
        ..., description="Of waso_minutes, the awake minutes after the last sleep interval"
    )
    awakenings: int = Field(..., description="Distinct awake episodes after onset; touching intervals count once")
    unaccounted_after_onset_minutes: float = Field(
        ...,
        description="After onset, minutes scored neither awake nor asleep (unknown, or no interval). Not in WASO.",
    )
    first_sleep_run_minutes: float = Field(..., description="Length of the unbroken sleep that begins at onset")
    short_first_sleep_run: bool = Field(
        ..., description=f"first_sleep_run_minutes is under {SHORT_FIRST_SLEEP_RUN_MINUTES}"
    )
    total_awake_minutes: float = Field(..., description="Every awake minute in the intervals, before onset included")


def _merge(spans: list[Span]) -> list[Span]:
    out: list[list[float]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


def _clip(spans: list[Span], lo: float, hi: float) -> list[Span]:
    return [(max(a, lo), min(b, hi)) for a, b in spans if b > lo and a < hi]


def _total(spans: list[Span]) -> float:
    return sum(b - a for a, b in spans)


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def derive_sleep_onset_metrics(
    stages: Sequence[SleepStage] | None,
    session_start: datetime | None,
) -> SleepOnsetMetrics | None:
    """Latency, WASO and their coverage from one session's stage intervals.

    Returns ``None`` when there are no intervals or none is scored as sleep.
    """
    if not stages:
        return None
    clean = [
        s for s in stages if s.stage != SleepStageType.IN_BED and s.end_time.timestamp() > s.start_time.timestamp()
    ]

    def spans_of(kinds: set[SleepStageType]) -> list[Span]:
        return _merge([(s.start_time.timestamp(), s.end_time.timestamp()) for s in clean if s.stage in kinds])

    asleep = spans_of(_SLEEP)
    if not asleep:
        return None
    awake = spans_of({SleepStageType.AWAKE})

    onset = asleep[0][0]
    trace_end = max(s.end_time.timestamp() for s in clean)
    last_sleep_end = asleep[-1][1]

    latency: float | None = None
    if session_start is not None:
        start = session_start.timestamp()
        if onset <= start:
            latency = 0.0
        else:
            covered = _total(_clip(awake, start, onset))
            if (onset - start) - covered <= GAP_TOLERANCE_SECONDS:
                latency = _minutes(onset - start)

    after_onset = _clip(awake, onset, trace_end)
    scored = _merge(after_onset + _clip(asleep, onset, trace_end))
    first_run = (asleep[0][1] - asleep[0][0]) / 60

    return SleepOnsetMetrics(
        onset_time=next(s.start_time for s in clean if s.stage in _SLEEP and s.start_time.timestamp() == onset),
        latency_minutes=latency,
        waso_minutes=_minutes(_total(after_onset)),
        terminal_wake_minutes=_minutes(_total(_clip(awake, last_sleep_end, trace_end))),
        awakenings=len(after_onset),
        unaccounted_after_onset_minutes=_minutes(max(0.0, trace_end - onset - _total(scored))),
        first_sleep_run_minutes=round(first_run, 1),
        short_first_sleep_run=first_run < SHORT_FIRST_SLEEP_RUN_MINUTES,
        total_awake_minutes=_minutes(_total(awake)),
    )
