"""Sleep-stage vocabularies accepted by the Apple ingest paths (pure, no DB).

The SDK path used to accept only the canonical snake_case values, so every
HealthKit-native label resolved to None and was skipped without a trace. That cost
an Apple Watch its entire sleep history — the watch writes asleepCore/asleepDeep/
asleepREM, while third-party apps relaying through Apple Health write labels that
happened to resolve, so the loss looked like a device-specific mystery.
"""

import pytest

from app.constants.series_types.sdk import SleepPhase, get_apple_sleep_phase


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # What an Apple Watch writes for a staged night (watchOS 9+).
        ("inBed", SleepPhase.IN_BED),
        ("asleepCore", SleepPhase.ASLEEP_LIGHT),
        ("asleepDeep", SleepPhase.ASLEEP_DEEP),
        ("asleepREM", SleepPhase.ASLEEP_REM),
        ("asleepUnspecified", SleepPhase.SLEEPING),
        ("awake", SleepPhase.AWAKE),
        # The long form in the Apple Health XML export.
        ("HKCategoryValueSleepAnalysisInBed", SleepPhase.IN_BED),
        ("HKCategoryValueSleepAnalysisAsleepCore", SleepPhase.ASLEEP_LIGHT),
        ("HKCategoryValueSleepAnalysisAsleepDeep", SleepPhase.ASLEEP_DEEP),
        ("HKCategoryValueSleepAnalysisAsleepREM", SleepPhase.ASLEEP_REM),
        ("HKCategoryValueSleepAnalysisAsleep", SleepPhase.SLEEPING),
        ("HKCategoryValueSleepAnalysisAsleepUnspecified", SleepPhase.SLEEPING),
        ("HKCategoryValueSleepAnalysisAwake", SleepPhase.AWAKE),
        # The canonical values a normalising client sends — unchanged.
        ("in_bed", SleepPhase.IN_BED),
        ("light", SleepPhase.ASLEEP_LIGHT),
        ("deep", SleepPhase.ASLEEP_DEEP),
        ("rem", SleepPhase.ASLEEP_REM),
        ("sleeping", SleepPhase.SLEEPING),
        # A provider that shouts its enum resolves too.
        ("DEEP", SleepPhase.ASLEEP_DEEP),
        ("Rem", SleepPhase.ASLEEP_REM),
        ("  light  ", SleepPhase.ASLEEP_LIGHT),
    ],
)
def test_known_stage_labels_resolve(label: str, expected: SleepPhase) -> None:
    assert get_apple_sleep_phase(label) == expected


@pytest.mark.parametrize("label", ["outOfBed", "banana", "", None])
def test_unknown_stage_labels_return_none_for_the_caller_to_report(label: str | None) -> None:
    """None is the signal to count and surface the record, never to skip quietly."""
    assert get_apple_sleep_phase(label) is None


def test_every_stage_that_can_open_a_session_is_reachable_from_healthkit() -> None:
    """A night must be openable from the labels an Apple Watch actually emits.

    ``awake`` alone cannot start a session, so if the asleep* labels do not resolve
    the state machine never opens one and the whole night is lost.
    """
    from app.schemas.providers.mobile_sdk import SLEEP_START_STATES

    watch_asleep_labels = ["inBed", "asleepCore", "asleepDeep", "asleepREM"]
    resolved = {get_apple_sleep_phase(label) for label in watch_asleep_labels}

    assert None not in resolved
    assert resolved <= SLEEP_START_STATES
