"""Reading an account's device history off the device names its provider already sends.

Garmin names the watch on every activity and never on a night's sleep, an HRV reading
or a day's steps; Polar, Suunto and Fitbit do the same. So an account's workouts
already say which watch it was worn with and when that changed, and the records that
carry no name can be filed by that history instead of by whatever is worn today.

The rules, each chosen over a more convenient alternative:

- **A switch is the first workout on the new device.** Workouts only bracket a switch:
  the old device's last one and the new device's first. Nights in between stay with the
  old device, so the new one is credited only from the first positive evidence of it.
  The dashboard shows that gap, so a person who knows the date can tighten it.
- **Every change is a switch.** No smoothing: one workout on another watch between two
  on the first reads as two switches. An account worn with one device at a time - the
  case this is for - never produces that, and smoothing it away would hide an account
  that is not worn that way.
- **Only something worn on the body overnight**: a watch, band or ring. A bike
  computer, a phone app or a chest strap records workouts but not the nights, so its
  name never starts a period.
- **Not a workout someone typed in.** A manual entry names no device - or names the
  account's label, filled in at ingest - and is evidence of nothing.
- **Only providers that name the device on workouts and nowhere else**
  (EVIDENCE_PROVIDERS). That is what lets a re-file move rows by a detected period: a
  row inside another device's detected period cannot be this device's own capture,
  because a capture of it would have been a workout and would have ended the period.
- **Names compare folded**: "fēnix 8", "Fenix 8" and "Garmin fenix 8" are one device.
  The period takes the provider's own spelling from its first workout.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from app.constants.devices_map import infer_device_type_from_model
from app.schemas.enums import DeviceType, ProviderName

# Providers whose device name is on workouts and on nothing else. Strava is not here:
# it holds no nights to file, and its activities come as often from a phone or a bike
# computer as from the watch. Apple, Samsung, Health Connect and Google Health name
# the device on every record already, so there is no gap for a history to fill.
EVIDENCE_PROVIDERS: frozenset[ProviderName] = frozenset(
    {ProviderName.GARMIN, ProviderName.POLAR, ProviderName.SUUNTO, ProviderName.FITBIT}
)

# Worn through the night, so the device the wellness records came from.
EVIDENCE_DEVICE_TYPES: frozenset[DeviceType] = frozenset({DeviceType.WATCH, DeviceType.BAND, DeviceType.RING})

_BRAND_PREFIX = re.compile(r"^(garmin|polar|suunto|fitbit|google|coros)\s+")


def device_key(device_model: str) -> str:
    """The name two spellings of one model share: case, accents, spacing and brand aside."""
    decomposed = unicodedata.normalize("NFKD", device_model)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    return _BRAND_PREFIX.sub("", " ".join(folded.split()))


def provider_detects_switches(provider: str | ProviderName) -> bool:
    try:
        return ProviderName(provider) in EVIDENCE_PROVIDERS
    except ValueError:
        return False


def is_evidence_device(device_model: str | None) -> bool:
    """Whether a workout named this device says what the account was worn with."""
    return infer_device_type_from_model(device_model) in EVIDENCE_DEVICE_TYPES


@dataclass(frozen=True)
class Evidence:
    """One workout the provider named a device on."""

    at: datetime
    device_model: str


@dataclass(frozen=True)
class DetectedPeriod:
    device_label: str
    # None: from the start of the account's data.
    effective_from: datetime | None
    # This run of workouts: how many, and the first and last of them.
    evidence_count: int
    first_seen: datetime
    last_seen: datetime
    # The previous device's last workout. The switch happened after it and no later
    # than this period's first workout; None for the first period.
    previous_last_seen: datetime | None


@dataclass(frozen=True)
class DeviceSighting:
    """Every workout one device recorded on the account, for display."""

    device_label: str
    evidence_count: int
    first_seen: datetime
    last_seen: datetime


def _utc(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def detect_periods(
    evidence: Sequence[Evidence],
    *,
    after: datetime | None = None,
    current_label: str | None = None,
) -> list[DetectedPeriod]:
    """The periods ``evidence`` shows, in order.

    With ``after`` None this is the account's whole history, and the first period runs
    from the start of its data: before the first workout nothing names a device, and
    the first one seen is the only candidate there is. With ``after`` set only workouts
    after it count, and they continue from ``current_label`` - the device the account's
    history names at that instant - so a run of the same device adds nothing.
    """
    ordered = sorted(
        (e for e in evidence if after is None or _utc(e.at) > _utc(after)),
        key=lambda e: _utc(e.at),
    )
    current_key = device_key(current_label) if current_label else None
    previous_last: datetime | None = None
    runs: list[list[Evidence]] = []
    for item in ordered:
        key = device_key(item.device_model)
        # Two devices at one instant cannot both start a period there; the first
        # one read keeps it rather than the history failing to save.
        if runs and (device_key(runs[-1][0].device_model) == key or _utc(item.at) == _utc(runs[-1][0].at)):
            runs[-1].append(item)
            continue
        if not runs and key == current_key:
            previous_last = _utc(item.at)
            continue
        runs.append([item])

    periods: list[DetectedPeriod] = []
    for i, run in enumerate(runs):
        first, last = _utc(run[0].at), _utc(run[-1].at)
        opens_history = i == 0 and after is None
        periods.append(
            DetectedPeriod(
                device_label=run[0].device_model,
                effective_from=None if opens_history else first,
                evidence_count=len(run),
                first_seen=first,
                last_seen=last,
                previous_last_seen=_utc(runs[i - 1][-1].at) if i > 0 else previous_last,
            )
        )
    return periods


def sightings(evidence: Sequence[Evidence]) -> list[DeviceSighting]:
    """Each device the evidence names, first-seen order, under its latest spelling."""
    by_key: dict[str, list[Evidence]] = {}
    for item in sorted(evidence, key=lambda e: _utc(e.at)):
        by_key.setdefault(device_key(item.device_model), []).append(item)
    return [
        DeviceSighting(
            device_label=items[-1].device_model,
            evidence_count=len(items),
            first_seen=_utc(items[0].at),
            last_seen=_utc(items[-1].at),
        )
        for items in by_key.values()
    ]
