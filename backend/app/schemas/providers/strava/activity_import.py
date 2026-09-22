from pydantic import BaseModel, ConfigDict


class StravaGearJSON(BaseModel):
    """Strava gear data from API responses."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    primary: bool
    name: str
    resource_state: int
    distance: int


class ActivityJSON(BaseModel):
    """Strava activity data from API responses or webhook fetches.

    Based on Strava API v3 DetailedActivity schema.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Required fields
    id: int
    name: str
    type: str  # e.g. "Run", "Ride", "Swim"
    sport_type: str  # More specific, e.g. "TrailRun", "MountainBikeRide"
    start_date: str  # ISO 8601 UTC
    elapsed_time: int  # seconds

    # Optional fields
    distance: float | None = None  # meters
    moving_time: int | None = None  # seconds
    total_elevation_gain: float | None = None  # meters
    elev_high: float | None = None  # meters
    elev_low: float | None = None  # meters

    # Heart rate
    average_heartrate: float | None = None
    max_heartrate: float | None = None
    has_heartrate: bool | None = None

    # Cadence (rpm for rides, spm per-leg for runs)
    average_cadence: float | None = None

    # Ambient temperature in Celsius. Only head units and watches with a thermometer
    # report it, so its presence is a device capability signal as much as a metric.
    average_temp: int | None = None

    # Speed
    average_speed: float | None = None  # meters/second
    max_speed: float | None = None  # meters/second

    # Power
    average_watts: float | None = None
    max_watts: int | None = None
    weighted_average_watts: int | None = None
    device_watts: bool | None = None

    # Calories & energy
    kilojoules: float | None = None
    calories: float | None = None

    # Athlete info
    athlete: dict | None = None  # {"id": 12345}

    # Position. Only presence is used - an empty list means the recorder had no GPS
    # fix, which separates a watch from a treadmill entry or a manual log.
    start_latlng: list[float] | None = None
    end_latlng: list[float] | None = None

    # Metadata
    gear: StravaGearJSON | None = None
    gear_id: str | None = None
    # The recorder's own name, e.g. "Garmin Forerunner 965". DetailedActivity only:
    # GET /athlete/activities (the backfill list endpoint) omits it, which is what
    # settings.strava_enrich_activity_detail exists to work around.
    device_name: str | None = None
    # The name the upload arrived under - "garmin_push_<id>" for a Garmin auto-sync, a
    # file name for an upload, absent for plenty of activities. Strava's own developer
    # forum describes it as inconsistent, so it is read as evidence about the upload
    # channel and never as an identifier. See services/providers/strava/device_provenance.py.
    external_id: str | None = None
    upload_id: int | None = None
    upload_id_str: str | None = None
    trainer: bool | None = None
    commute: bool | None = None
    manual: bool | None = None
    private: bool | None = None
    # True when the activity was created from a tag on someone else's upload, so its
    # device fields describe the tagger's recorder and not this athlete's.
    from_accepted_tag: bool | None = None

    # Timestamps
    start_date_local: str | None = None  # ISO 8601 local
    timezone: str | None = None
    utc_offset: float | None = None
