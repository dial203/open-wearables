"""Work out what recorded a Strava activity, from the metadata Strava actually sends.

Strava is an aggregator: almost nothing on it was recorded by Strava. An activity
arrives from a Garmin watch, a Wahoo head unit, Zwift, a phone, or a file somebody
dragged onto the website, and the platform keeps only a thin trace of which. For a
study running several Strava accounts - one per wearable - that trace is the
difference between "this account's data" and "this device's data".

What Strava gives us, in descending order of usefulness:

``device_name``
    The recorder's own name ("Garmin Forerunner 965", "Wahoo ELEMNT BOLT"). The only
    field that names hardware. **Present on the detail endpoint only**
    (``GET /activities/{id}``); the list endpoint used for backfill omits it, which is
    why ``settings.strava_enrich_activity_detail`` exists.

``external_id``
    The name the upload came in under. Heterogeneous by design: a Garmin auto-sync
    gives ``garmin_push_<id>``, a file upload gives the file name, and plenty of
    activities carry nothing at all. Strava's own developer forum is explicit that it
    "sometimes references a ping record, sometimes a FIT file, and sometimes is
    empty", so it is treated here as evidence, never as an identifier.

``device_watts``, ``trainer``, ``has_heartrate``, ``start_latlng``, ``average_temp``
    Capability flags. They do not name a device but they constrain it, and
    ``device_watts`` in particular separates a measured power trace from Strava's own
    estimate - which matters more than the device name if the numbers are going into
    an analysis.

**What this module will not do.** It will not turn an upload source into a device
model. "garmin_push" names the sync channel, and one athlete's Edge and Forerunner
both upload through it; writing "Garmin" into ``device_model`` would pool them into a
single unit with no visible symptom. That is the over-merge
``app/services/devices/detection.py`` exists to prevent, and the rule holds here: an
upload source is a WEAK identity claim and a brand, nothing more. Only a name Strava
reported for the hardware itself reaches ``device_model``.

When no device can be named, the honest answer is None - which lets
``DataSourceRepository.ensure_data_source`` fall back to the connection's
``device_label``. For a validation setup that is the intended path: label the Strava
account with the unit it is paired to and every device-less activity from it inherits
that label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from app.schemas.enums import DeviceType, infer_device_type_from_model
from app.schemas.providers.strava import ActivityJSON as StravaActivityJSON


class RecordingMode(StrEnum):
    """How the activity came to exist, which bounds what its numbers can mean."""

    DEVICE = "device"  # recorded by hardware that was actually worn or ridden
    PHONE = "phone"  # recorded by the Strava mobile app
    VIRTUAL = "virtual"  # indoor trainer or a virtual platform (Zwift, Rouvy, ...)
    FILE_UPLOAD = "file_upload"  # a .fit/.tcx/.gpx somebody uploaded; recorder unknown
    MANUAL = "manual"  # typed in by hand; no sensor involved at all
    UNKNOWN = "unknown"


class PowerSource(StrEnum):
    """Whether a power trace was measured or inferred.

    Strava estimates power for any activity with distance and elevation, and reports
    it in the same ``average_watts`` field a real meter fills. ``device_watts``
    distinguishes them, and nothing downstream can once the value is stored.
    """

    METER = "meter"  # device_watts = true: a power meter was connected
    ESTIMATED = "estimated"  # Strava's own model, not a measurement
    NONE = "none"


# --- Upload-source patterns ------------------------------------------------------
#
# Matched against `external_id`, the name the upload arrived under.
#
# VERIFIED covers the forms confirmed against Strava's own documentation and
# developer forum. Everything else is PROVISIONAL: the shape has been reported by
# users but not confirmed by the vendor, so it produces a brand and a label and is
# marked as such in the evidence, and a wrong guess costs a label somebody corrects
# rather than a merge nobody sees.
#
# Extending this table is the intended maintenance path. Unmatched prefixes are
# logged by `workouts.py` under action="strava_upload_source_unmapped" with the raw
# token, so a deployment can read its own uploads and add what it finds.

_UploadRule = tuple[re.Pattern[str], str, str, str]  # pattern, token, label, brand

_VERIFIED_UPLOAD_RULES: tuple[_UploadRule, ...] = (
    # Garmin Connect's auto-sync. Both spellings are in circulation; the numeric tail
    # is Garmin's own upload id and is deliberately not captured - it identifies the
    # upload, not the watch.
    (re.compile(r"^garmin_(push|ping)_", re.I), "garmin_connect", "Garmin Connect", "Garmin"),
)

_PROVISIONAL_UPLOAD_RULES: tuple[_UploadRule, ...] = (
    (re.compile(r"^zwift", re.I), "zwift", "Zwift", "Zwift"),
    (re.compile(r"^rouvy", re.I), "rouvy", "Rouvy", "Rouvy"),
    (re.compile(r"^trainerroad", re.I), "trainerroad", "TrainerRoad", "TrainerRoad"),
    (re.compile(r"^peloton", re.I), "peloton", "Peloton", "Peloton"),
    (re.compile(r"^wahoo", re.I), "wahoo", "Wahoo", "Wahoo"),
    (re.compile(r"^coros", re.I), "coros", "COROS", "COROS"),
    (re.compile(r"^suunto", re.I), "suunto", "Suunto", "Suunto"),
    (re.compile(r"^polar", re.I), "polar", "Polar Flow", "Polar"),
    (re.compile(r"^whoop", re.I), "whoop", "Whoop", "Whoop"),
    (re.compile(r"^concept2", re.I), "concept2", "Concept2", "Concept2"),
    (re.compile(r"^hammerhead|^karoo", re.I), "hammerhead", "Hammerhead Karoo", "Hammerhead"),
)

# Brand keywords looked for *inside* a file name, for uploads that arrive as a file
# rather than under a vendor prefix. Wahoo names its files after the head unit
# ("...-ELEMNT BOLT-..."), which is the case this exists for. Substring match on the
# case-folded name, longest keyword first so "elemnt bolt" wins over "elemnt".
_FILENAME_BRAND_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("elemnt", "Wahoo"),
    ("wahoo", "Wahoo"),
    ("garmin", "Garmin"),
    ("forerunner", "Garmin"),
    ("fenix", "Garmin"),
    ("edge", "Garmin"),
    ("coros", "COROS"),
    ("suunto", "Suunto"),
    ("polar", "Polar"),
    ("zwift", "Zwift"),
    ("apple", "Apple"),
    ("amazfit", "Zepp"),
    ("zepp", "Zepp"),
    ("samsung", "Samsung"),
)

_FILE_SUFFIXES: tuple[str, ...] = (".fit", ".tcx", ".gpx", ".fit.gz", ".tcx.gz", ".gpx.gz")

# `device_name` values that name the upload channel rather than any hardware. A
# website file upload reports "Strava GPX"; using it as a device model would pool
# every hand-uploaded file - from any number of real devices - into one unit.
_NON_DEVICE_NAMES: frozenset[str] = frozenset({"strava gpx", "strava tcx", "strava fit", "strava"})

# `device_name` values that name the Strava mobile app. These *are* a device: the
# handset recorded the activity itself, so the name is kept as the model.
_PHONE_APP_RE = re.compile(r"^strava (iphone|android|ios) app$", re.I)

# The mobile app, named explicitly so it does not fall through to the brand-derived
# label below and produce "Strava" - which differs from the provider literal
# "strava" only by case and would key a second, duplicate data source.
_STRAVA_APP_TOKEN = "strava_app"
_STRAVA_APP_LABEL = "Strava App"

# Sport types Strava files as virtual. Matched as a prefix so new "Virtual*" sports
# do not need a code change.
_VIRTUAL_SPORT_PREFIX = "virtual"

# Brand keywords for a reported `device_name`. Independent of the file-name table
# above because the strings differ in kind: this one sees marketing names, that one
# sees file names.
_DEVICE_NAME_BRANDS: tuple[tuple[str, str], ...] = (
    ("garmin", "Garmin"),
    ("forerunner", "Garmin"),
    ("fenix", "Garmin"),
    ("epix", "Garmin"),
    ("venu", "Garmin"),
    ("instinct", "Garmin"),
    ("enduro", "Garmin"),
    ("tactix", "Garmin"),
    ("edge", "Garmin"),
    ("wahoo", "Wahoo"),
    ("elemnt", "Wahoo"),
    ("coros", "COROS"),
    ("apex", "COROS"),
    ("pace 3", "COROS"),
    ("vertix", "COROS"),
    ("suunto", "Suunto"),
    ("polar", "Polar"),
    ("vantage", "Polar"),
    ("grit x", "Polar"),
    ("apple watch", "Apple"),
    ("whoop", "Whoop"),
    ("fitbit", "Fitbit"),
    ("amazfit", "Zepp"),
    ("zepp", "Zepp"),
    ("hammerhead", "Hammerhead"),
    ("karoo", "Hammerhead"),
    ("zwift", "Zwift"),
    ("peloton", "Peloton"),
    ("strava", "Strava"),
)


@dataclass(frozen=True)
class StravaProvenance:
    """Everything the payload lets us say about what recorded one activity.

    ``device_model`` is the only field that may become a grouping key, and it is
    non-null only when Strava named the hardware. The rest is evidence: it labels,
    it brands, it supports a link proposal, and it never merges two units.
    """

    device_model: str | None
    upload_source: str | None  # normalized token, e.g. "garmin_connect"
    upload_source_label: str | None  # display form, e.g. "Garmin Connect"
    brand: str | None
    device_type: DeviceType
    recording: RecordingMode
    power_source: PowerSource
    sensors: frozenset[str]
    # Why each conclusion was reached, and what did not match. Carried into the
    # structured log so an unmapped upload source is findable in production rather
    # than silently dropped.
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def data_source_label(self) -> str:
        """What to file this activity's data source under.

        The upload source when we have one, because on an aggregator route that is
        what the ``source`` column means everywhere else in this codebase: the thing
        that wrote the data, as distinct from the platform that carried it. Falling
        back to the bare provider literal keeps pre-existing rows keyed as they were.
        """
        return self.upload_source_label or "strava"

    @property
    def is_measured_power(self) -> bool:
        return self.power_source is PowerSource.METER


def upload_prefix(external_id: str | None) -> str | None:
    """The leading token of an ``external_id``, for reporting what we failed to map.

    ``"garmin_push_1234"`` -> ``"garmin_push"``, ``"zwift-activity-9.fit"`` ->
    ``"zwift"``, ``"track(1).gpx"`` -> ``"track"``. Digits are dropped from the tail so
    one athlete's uploads collapse to a single token instead of one per activity.
    """
    if not external_id:
        return None
    stem = external_id.strip()
    for suffix in _FILE_SUFFIXES:
        if stem.casefold().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    # Split on the separators upload names actually use, then keep the leading
    # non-numeric run: "garmin_push_1234" -> ["garmin", "push", "1234"].
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", stem) if p]
    leading = [p for p in parts if not p.isdigit()][:2]
    if not leading:
        return None
    return "_".join(leading).casefold()[:64]


def _match_upload_rules(external_id: str) -> tuple[str, str, str, str] | None:
    """(token, label, brand, verification) for the first rule that matches."""
    for pattern, token, label, brand in _VERIFIED_UPLOAD_RULES:
        if pattern.search(external_id):
            return token, label, brand, "verified"
    for pattern, token, label, brand in _PROVISIONAL_UPLOAD_RULES:
        if pattern.search(external_id):
            return token, label, brand, "provisional"
    return None


def _looks_like_file(external_id: str) -> bool:
    lowered = external_id.casefold()
    return any(lowered.endswith(suffix) for suffix in _FILE_SUFFIXES)


def _brand_in_filename(external_id: str) -> str | None:
    lowered = external_id.casefold()
    for keyword, brand in _FILENAME_BRAND_KEYWORDS:
        if keyword in lowered:
            return brand
    return None


def _brand_from_device_name(device_name: str) -> str | None:
    lowered = device_name.casefold()
    for keyword, brand in _DEVICE_NAME_BRANDS:
        if keyword in lowered:
            return brand
    return None


def _reported_device_model(device_name: str | None) -> str | None:
    """The reported name, when it names hardware rather than an upload channel."""
    if not device_name:
        return None
    cleaned = device_name.strip()
    if not cleaned or cleaned.casefold() in _NON_DEVICE_NAMES:
        return None
    return cleaned


def _sensors(activity: StravaActivityJSON) -> frozenset[str]:
    """Which signals the recording actually carried.

    Capability rather than identity, but it is the part a validation study cares
    about: an activity with no ``heart_rate`` here has no heart rate however good the
    watch was, and one with estimated power has no power at all.
    """
    present: set[str] = set()
    if activity.has_heartrate or activity.average_heartrate is not None:
        present.add("heart_rate")
    if activity.device_watts:
        present.add("power")
    if activity.average_cadence is not None:
        present.add("cadence")
    if activity.average_temp is not None:
        present.add("temperature")
    if activity.start_latlng or activity.end_latlng:
        present.add("gps")
    return frozenset(present)


def _power_source(activity: StravaActivityJSON) -> PowerSource:
    if activity.device_watts:
        return PowerSource.METER
    if activity.average_watts is not None or activity.max_watts is not None or activity.kilojoules is not None:
        return PowerSource.ESTIMATED
    return PowerSource.NONE


def _recording_mode(
    activity: StravaActivityJSON,
    device_model: str | None,
    upload_token: str | None,
    is_file_upload: bool,
) -> RecordingMode:
    if activity.manual:
        return RecordingMode.MANUAL
    sport = (activity.sport_type or activity.type or "").casefold()
    if sport.startswith(_VIRTUAL_SPORT_PREFIX) or upload_token in {"zwift", "rouvy", "trainerroad", "peloton"}:
        return RecordingMode.VIRTUAL
    if device_model and _PHONE_APP_RE.match(device_model):
        return RecordingMode.PHONE
    if device_model:
        return RecordingMode.DEVICE
    if is_file_upload:
        return RecordingMode.FILE_UPLOAD
    if upload_token:
        return RecordingMode.DEVICE
    # `trainer` is checked last: a trainer ride recorded on a head unit is still that
    # head unit's recording, and only becomes "virtual" when nothing else named a
    # recorder.
    if activity.trainer:
        return RecordingMode.VIRTUAL
    return RecordingMode.UNKNOWN


def derive_provenance(activity: StravaActivityJSON) -> StravaProvenance:
    """Everything the activity payload supports saying about its recorder.

    Pure: no I/O, no database, no settings. Every conclusion is accompanied by the
    field that produced it in ``evidence``, so a wrong answer can be traced to the
    rule that produced it rather than guessed at.
    """
    evidence: dict[str, str] = {}

    device_model = _reported_device_model(activity.device_name)
    if activity.device_name:
        evidence["device_name"] = activity.device_name[:255]
        if device_model is None:
            # "Strava GPX" and friends: reported, but naming the upload channel.
            evidence["device_name_verdict"] = "upload_channel_not_hardware"

    external_id = (activity.external_id or "").strip()
    upload_token: str | None = None
    upload_label: str | None = None
    brand: str | None = None
    is_file_upload = False

    if external_id:
        evidence["external_id"] = external_id[:255]
        matched = _match_upload_rules(external_id)
        if matched:
            upload_token, upload_label, brand, verification = matched
            evidence["upload_source_match"] = verification
        else:
            is_file_upload = _looks_like_file(external_id)
            if is_file_upload:
                upload_token, upload_label = "file_upload", "File upload"
                brand = _brand_in_filename(external_id)
                evidence["upload_source_match"] = "file_name"
            if brand is None:
                # Nothing recognised the uploader. Record the token so the pattern
                # table can be extended from what a deployment actually receives;
                # a file whose name did name a brand is already handled.
                prefix = upload_prefix(external_id)
                if prefix:
                    evidence["upload_source_prefix_unmapped"] = prefix

    # A reported device name outranks anything read out of a file name: Strava got it
    # from the uploading vendor, we got the other by pattern matching.
    if device_model:
        brand = _brand_from_device_name(device_model) or brand
    elif activity.device_name and upload_label is None:
        # "Strava GPX" says the athlete uploaded a file, and nothing more: the brand
        # stays unset because a hand-uploaded file could have come off anything.
        upload_token, upload_label = "file_upload", "File upload"
        evidence["upload_source_match"] = "device_name"

    is_phone_app = bool(device_model and _PHONE_APP_RE.match(device_model))
    if is_phone_app:
        upload_token, upload_label = _STRAVA_APP_TOKEN, _STRAVA_APP_LABEL
        evidence["upload_source_match"] = "strava_mobile_app"
    elif device_model and upload_label is None and brand:
        # Nothing in external_id, but the recorder named itself - the brand's own sync
        # is the only way that happens, so label it by brand rather than leaving the
        # data source under the bare provider literal.
        upload_token = brand.casefold().replace(" ", "_")
        upload_label = brand
        evidence["upload_source_match"] = "device_name_brand"

    device_type = DeviceType.PHONE if is_phone_app else DeviceType.UNKNOWN
    if device_model and not is_phone_app:
        device_type = infer_device_type_from_model(device_model)

    power_source = _power_source(activity)
    recording = _recording_mode(activity, device_model, upload_token, is_file_upload)
    if activity.trainer:
        evidence["trainer"] = "true"
    evidence["power_source"] = power_source.value
    evidence["recording"] = recording.value

    return StravaProvenance(
        device_model=device_model,
        upload_source=upload_token,
        upload_source_label=upload_label,
        brand=brand,
        device_type=device_type,
        recording=recording,
        power_source=power_source,
        sensors=_sensors(activity),
        evidence=evidence,
    )
