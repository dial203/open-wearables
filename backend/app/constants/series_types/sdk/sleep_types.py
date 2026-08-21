from enum import StrEnum

from app.constants.sleep import SleepStageType


class SleepPhase(StrEnum):
    IN_BED = SleepStageType.IN_BED
    SLEEPING = "sleeping"
    AWAKE = SleepStageType.AWAKE
    ASLEEP_LIGHT = SleepStageType.LIGHT
    ASLEEP_DEEP = SleepStageType.DEEP
    ASLEEP_REM = SleepStageType.REM
    UNKNOWN = SleepStageType.UNKNOWN


# Provider sleep-stage vocabularies, keyed casefolded.
#
# The canonical values above are what a normalising client sends. HealthKit's own
# vocabulary is not that, and arrives in two shapes: the long
# ``HKCategoryValueSleepAnalysis*`` names in the Apple Health XML export, and the
# short camelCase names the mobile SDKs surface. ``asleepCore``/``asleepDeep``/
# ``asleepREM`` are what an Apple Watch writes for a staged night (watchOS 9+), so
# a backend that cannot read them loses the watch's sleep entirely while every
# third-party app relaying through Apple Health continues to work.
_SLEEP_PHASE_ALIASES: dict[str, SleepPhase] = {
    # HealthKit, long form (Apple Health XML export)
    "hkcategoryvaluesleepanalysisinbed": SleepPhase.IN_BED,
    "hkcategoryvaluesleepanalysisawake": SleepPhase.AWAKE,
    "hkcategoryvaluesleepanalysisasleep": SleepPhase.SLEEPING,
    "hkcategoryvaluesleepanalysisasleepunspecified": SleepPhase.SLEEPING,
    "hkcategoryvaluesleepanalysisasleepcore": SleepPhase.ASLEEP_LIGHT,
    "hkcategoryvaluesleepanalysisasleepdeep": SleepPhase.ASLEEP_DEEP,
    "hkcategoryvaluesleepanalysisasleeprem": SleepPhase.ASLEEP_REM,
    # HealthKit, short form (mobile SDK payloads)
    "inbed": SleepPhase.IN_BED,
    "asleep": SleepPhase.SLEEPING,
    "asleepunspecified": SleepPhase.SLEEPING,
    "asleepcore": SleepPhase.ASLEEP_LIGHT,
    "asleepdeep": SleepPhase.ASLEEP_DEEP,
    "asleeprem": SleepPhase.ASLEEP_REM,
    # Apple's own core/deep/rem shorthand, seen without the "asleep" prefix
    "core": SleepPhase.ASLEEP_LIGHT,
}


def get_apple_sleep_phase(apple_sleep_phase: str | None) -> SleepPhase | None:
    """Resolve a provider's sleep-stage label to a SleepPhase.

    Accepts the canonical values and HealthKit's own vocabulary in both its long and
    short forms. Matching is case-insensitive, so a provider that shouts its enum
    (Health Connect's ``DEEP``) resolves too.

    Returns None when nothing matches. Callers must *report* that rather than skip
    silently: an unreadable stage means a night is discarded, and when the counter
    reporting the outcome counts the input instead of what persisted, the loss is
    invisible from both the logs and the API response.
    """
    if not apple_sleep_phase:
        return None

    key = apple_sleep_phase.strip().casefold()
    try:
        return SleepPhase(key)
    except ValueError:
        return _SLEEP_PHASE_ALIASES.get(key)
