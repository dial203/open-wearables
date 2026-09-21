"""Response models for Polar AccessLink v4 (``/v4/data``).

Only the slices Open Wearables reads are modelled: training-session RR samples and
pulse-to-pulse interval samples. v4 is camelCase throughout, unlike v3's kebab-case.
"""

from pydantic import BaseModel, ConfigDict, Field


class V4Model(BaseModel):
    """v4 payloads carry far more than we read; ignore the rest rather than fail on it."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class RRSampleJSON(V4Model):
    """One heartbeat.

    Unlike v3's sample type 11 — a bare comma-separated list whose missing beats are
    untimed NULLs — every v4 beat carries its own duration, including the ones recorded
    while the strap lost contact. The beat clock is therefore exact, with nothing estimated.
    """

    duration_millis: int = Field(alias="durationMillis")
    offline: bool = False


class TrainingSessionSamplesJSON(V4Model):
    rr_samples: list[RRSampleJSON] | None = Field(None, alias="rrSamples")


class TrainingSessionExerciseJSON(V4Model):
    start_time: str | None = Field(None, alias="startTime")
    timezone_offset_minutes: int | None = Field(None, alias="timezoneOffsetMinutes")
    samples: TrainingSessionSamplesJSON | None = None


class ProductReferenceJSON(V4Model):
    model_name: str | None = Field(None, alias="modelName")


class TrainingSessionJSON(V4Model):
    start_time: str | None = Field(None, alias="startTime")
    stop_time: str | None = Field(None, alias="stopTime")
    duration_millis: int | None = Field(None, alias="durationMillis")
    timezone_offset_minutes: int | None = Field(None, alias="timezoneOffsetMinutes")
    device_id: str | None = Field(None, alias="deviceId")
    product: ProductReferenceJSON | None = None
    exercises: list[TrainingSessionExerciseJSON] | None = None


class ListTrainingSessionsResponseJSON(V4Model):
    training_sessions: list[TrainingSessionJSON] | None = Field(None, alias="trainingSessions")


class PpiSampleJSON(V4Model):
    """One optical pulse interval, with the quality flags v3 never exposed."""

    offset_millis: int = Field(alias="offsetMillis")
    pp_interval: int = Field(alias="ppInterval")
    error_estimate_millis: int | None = Field(None, alias="errorEstimateMillis")
    skin_contact: bool = Field(True, alias="skinContact")
    movement: bool = False
    offline: bool = False


class PpiSamplesPerDeviceJSON(V4Model):
    ppi_samples: list[PpiSampleJSON] | None = Field(None, alias="ppiSamples")


class DailyPpiSamplesJSON(V4Model):
    date: str | None = None
    ppi_samples_per_device: list[PpiSamplesPerDeviceJSON] | None = Field(None, alias="ppiSamplesPerDevice")


class ListPpiSamplesResponseJSON(V4Model):
    daily_ppi_samples: list[DailyPpiSamplesJSON] | None = Field(None, alias="dailyPpiSamples")
