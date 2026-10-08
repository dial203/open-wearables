"""Which feed wrote a time-series sample, and which feed wins when two meet.

A row in ``data_point_series`` is keyed on (data source, series type, second). A
provider with two feeds for one series - a workout's per-second heart rate and the
same watch's all-day monitoring, say - resolves both to one data source, so the key
cannot tell them apart and whichever arrived last used to own every second both had.
That is how a Garmin run came back with one-minute averages on every :00/:15/:30/:45
second, and nothing on the row could show it afterwards.

Every writer now states its route. The route is stored on the row (``route_id``), so a
reader can tell a workout's trace from the monitoring around it, and an audit can check
what wrote each second. The route's kind ranks it: on a collision between two samples
(neither a daily total), a lower-ranked feed never overwrites a higher-ranked one. A
daily total and a sample at the same instant keep the previous last-write behaviour -
they are different measurements, and ranking one over the other would only choose
which to lose.

Route ids are stored in the database: never renumber one, never reuse a retired id.
"""

from enum import StrEnum


class RouteKind(StrEnum):
    """What a route's samples are, from most to least granular."""

    WORKOUT = "workout"  # a recording's own per-sample trace (activity streams, exercise RR)
    INTRADAY = "intraday"  # point samples from continuous monitoring or a spot measurement
    WINDOW = "window"  # one value summarising a window of seconds to minutes
    SUMMARY = "summary"  # one value for a whole day, night or workout


ROUTE_KIND_RANK: dict[RouteKind, int] = {
    RouteKind.WORKOUT: 40,
    RouteKind.INTRADAY: 30,
    RouteKind.WINDOW: 20,
    RouteKind.SUMMARY: 10,
}
# A row written before routes existed, or by a writer that states none.
UNKNOWN_ROUTE_RANK = 0


class SampleRoute(StrEnum):
    # Garmin Health API
    GARMIN_ACTIVITY_DETAILS = "garmin.activity_details"
    GARMIN_DAILIES = "garmin.dailies"
    GARMIN_EPOCHS = "garmin.epochs"
    GARMIN_HEALTH_SNAPSHOT = "garmin.health_snapshot"
    GARMIN_HRV = "garmin.hrv"
    GARMIN_STRESS = "garmin.stress"
    GARMIN_RESPIRATION = "garmin.respiration"
    GARMIN_PULSE_OX = "garmin.pulse_ox"
    GARMIN_BODY_COMP = "garmin.body_comp"
    GARMIN_BLOOD_PRESSURE = "garmin.blood_pressure"
    GARMIN_USER_METRICS = "garmin.user_metrics"
    GARMIN_SKIN_TEMP = "garmin.skin_temp"
    # Any FIT file's record messages (Garmin activityFiles today)
    FIT_RECORDS = "fit.records"
    # Polar AccessLink
    POLAR_CONTINUOUS_HR = "polar.continuous_hr"
    POLAR_SLEEP = "polar.sleep"
    POLAR_DAILY_ACTIVITY = "polar.daily_activity"
    POLAR_NIGHTLY_RECHARGE = "polar.nightly_recharge"
    POLAR_BODY_TEMPERATURE = "polar.body_temperature"
    POLAR_SLEEP_SKIN_TEMPERATURE = "polar.sleep_skin_temperature"
    POLAR_SPO2_TEST = "polar.spo2_test"
    POLAR_WRIST_ECG = "polar.wrist_ecg"
    POLAR_EXERCISE = "polar.exercise"
    POLAR_PPI = "polar.ppi"
    POLAR_RR_IMPORT = "polar.rr_import"
    # Oura
    OURA_HEARTRATE = "oura.heartrate"
    OURA_SLEEP = "oura.sleep"
    OURA_ACTIVITY = "oura.activity"
    OURA_DAILY = "oura.daily"
    # Strava
    STRAVA_STREAMS = "strava.streams"
    # Suunto
    SUUNTO_ACTIVITY_247 = "suunto.activity_247"
    SUUNTO_SLEEP = "suunto.sleep"
    SUUNTO_DAILY = "suunto.daily"
    # Ultrahuman
    ULTRAHUMAN_METRICS = "ultrahuman.metrics"
    ULTRAHUMAN_DAILY = "ultrahuman.daily"
    # WHOOP
    WHOOP_DAILY = "whoop.daily"
    # Google Health API
    GOOGLE_HEALTH_SAMPLES = "google_health.samples"
    GOOGLE_HEALTH_DAILY = "google_health.daily"
    # Withings
    WITHINGS_MEASURE = "withings.measure"
    WITHINGS_ACTIVITY = "withings.activity"
    WITHINGS_INTRADAY = "withings.intraday"
    WITHINGS_SLEEP = "withings.sleep"
    # SensorBio
    SENSORBIO_BIOMETRICS = "sensorbio.biometrics"
    SENSORBIO_DAILY = "sensorbio.daily"
    # Mobile SDK (Apple HealthKit, Health Connect, Samsung Health) and the Apple export
    SDK_RECORDS = "sdk.records"
    SDK_WORKOUT_STATISTICS = "sdk.workout_statistics"
    APPLE_XML_RECORDS = "apple_xml.records"


# (id, route, kind). The id is what data_point_series.route_id stores.
SAMPLE_ROUTE_DEFINITIONS: list[tuple[int, SampleRoute, RouteKind]] = [
    (1, SampleRoute.GARMIN_ACTIVITY_DETAILS, RouteKind.WORKOUT),
    (2, SampleRoute.GARMIN_DAILIES, RouteKind.WINDOW),
    (3, SampleRoute.GARMIN_EPOCHS, RouteKind.WINDOW),
    (4, SampleRoute.GARMIN_HEALTH_SNAPSHOT, RouteKind.WINDOW),
    (5, SampleRoute.GARMIN_HRV, RouteKind.WINDOW),
    (6, SampleRoute.GARMIN_STRESS, RouteKind.WINDOW),
    (7, SampleRoute.GARMIN_RESPIRATION, RouteKind.WINDOW),
    (8, SampleRoute.GARMIN_PULSE_OX, RouteKind.INTRADAY),
    (9, SampleRoute.GARMIN_BODY_COMP, RouteKind.INTRADAY),
    (10, SampleRoute.GARMIN_BLOOD_PRESSURE, RouteKind.INTRADAY),
    (11, SampleRoute.GARMIN_USER_METRICS, RouteKind.SUMMARY),
    (12, SampleRoute.GARMIN_SKIN_TEMP, RouteKind.SUMMARY),
    (13, SampleRoute.FIT_RECORDS, RouteKind.WORKOUT),
    (20, SampleRoute.POLAR_CONTINUOUS_HR, RouteKind.INTRADAY),
    (21, SampleRoute.POLAR_SLEEP, RouteKind.WINDOW),
    (22, SampleRoute.POLAR_DAILY_ACTIVITY, RouteKind.WINDOW),
    (23, SampleRoute.POLAR_NIGHTLY_RECHARGE, RouteKind.WINDOW),
    (24, SampleRoute.POLAR_BODY_TEMPERATURE, RouteKind.WINDOW),
    (25, SampleRoute.POLAR_SLEEP_SKIN_TEMPERATURE, RouteKind.WINDOW),
    (26, SampleRoute.POLAR_SPO2_TEST, RouteKind.WINDOW),
    (27, SampleRoute.POLAR_WRIST_ECG, RouteKind.WINDOW),
    (28, SampleRoute.POLAR_EXERCISE, RouteKind.WORKOUT),
    (29, SampleRoute.POLAR_PPI, RouteKind.INTRADAY),
    (30, SampleRoute.POLAR_RR_IMPORT, RouteKind.WORKOUT),
    (40, SampleRoute.OURA_HEARTRATE, RouteKind.INTRADAY),
    (41, SampleRoute.OURA_SLEEP, RouteKind.WINDOW),
    (42, SampleRoute.OURA_ACTIVITY, RouteKind.WINDOW),
    (43, SampleRoute.OURA_DAILY, RouteKind.SUMMARY),
    (50, SampleRoute.STRAVA_STREAMS, RouteKind.WORKOUT),
    (60, SampleRoute.SUUNTO_ACTIVITY_247, RouteKind.INTRADAY),
    (61, SampleRoute.SUUNTO_SLEEP, RouteKind.SUMMARY),
    (62, SampleRoute.SUUNTO_DAILY, RouteKind.SUMMARY),
    (70, SampleRoute.ULTRAHUMAN_METRICS, RouteKind.INTRADAY),
    (71, SampleRoute.ULTRAHUMAN_DAILY, RouteKind.SUMMARY),
    (80, SampleRoute.WHOOP_DAILY, RouteKind.SUMMARY),
    (90, SampleRoute.GOOGLE_HEALTH_SAMPLES, RouteKind.INTRADAY),
    (91, SampleRoute.GOOGLE_HEALTH_DAILY, RouteKind.SUMMARY),
    (100, SampleRoute.WITHINGS_MEASURE, RouteKind.INTRADAY),
    (101, SampleRoute.WITHINGS_ACTIVITY, RouteKind.SUMMARY),
    (102, SampleRoute.WITHINGS_INTRADAY, RouteKind.WINDOW),
    (103, SampleRoute.WITHINGS_SLEEP, RouteKind.SUMMARY),
    (110, SampleRoute.SENSORBIO_BIOMETRICS, RouteKind.INTRADAY),
    (111, SampleRoute.SENSORBIO_DAILY, RouteKind.SUMMARY),
    (120, SampleRoute.SDK_RECORDS, RouteKind.INTRADAY),
    (121, SampleRoute.SDK_WORKOUT_STATISTICS, RouteKind.SUMMARY),
    (130, SampleRoute.APPLE_XML_RECORDS, RouteKind.INTRADAY),
]

ROUTE_ID_BY_ROUTE: dict[SampleRoute, int] = {route: route_id for route_id, route, _ in SAMPLE_ROUTE_DEFINITIONS}
ROUTE_BY_ID: dict[int, SampleRoute] = {route_id: route for route_id, route, _ in SAMPLE_ROUTE_DEFINITIONS}
ROUTE_KIND_BY_ROUTE: dict[SampleRoute, RouteKind] = {route: kind for _, route, kind in SAMPLE_ROUTE_DEFINITIONS}
ROUTE_RANK_BY_ID: dict[int, int] = {route_id: ROUTE_KIND_RANK[kind] for route_id, _, kind in SAMPLE_ROUTE_DEFINITIONS}


def route_id_for(route: SampleRoute | None) -> int | None:
    """The stored id of a route, or None for a writer that stated none."""
    return ROUTE_ID_BY_ROUTE[route] if route is not None else None


def route_rank(route_id: int | None) -> int:
    """How a stored route id ranks on a collision; an unknown or missing route ranks lowest."""
    if route_id is None:
        return UNKNOWN_ROUTE_RANK
    return ROUTE_RANK_BY_ID.get(route_id, UNKNOWN_ROUTE_RANK)


def route_rank_sql(column: str) -> str:
    """``route_rank`` as a SQL CASE over ``column``, built from the table above."""
    whens = " ".join(f"WHEN {route_id} THEN {rank}" for route_id, rank in sorted(ROUTE_RANK_BY_ID.items()))
    return f"(CASE {column} {whens} ELSE {UNKNOWN_ROUTE_RANK} END)"
