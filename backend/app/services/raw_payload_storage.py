"""Optional raw payload storage for debugging incoming data.

Stores raw payloads received from SDKs, webhooks, and API responses.
Disabled by default - enable via RAW_PAYLOAD_STORAGE env var.

Supported backends:
    - "disabled" (default): no-op
    - "log": prints JSON to stdout
    - "s3": uploads to S3 bucket (configured via RAW_PAYLOAD_S3_BUCKET / AWS creds)

Usage (one-liner at ingestion point):
    store_raw_payload(source="webhook", provider="garmin", payload=data)
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.services.s3_client import create_s3_client
from app.utils.structured_logging import json_serial, log_structured

logger = logging.getLogger(__name__)

_storage_backend: str = "disabled"
_max_size_bytes: int = 10 * 1024 * 1024  # 10 MB
_s3_bucket: str | None = None
_s3_prefix: str = "raw-payloads"
_s3_client: Any = None
_fit_files_enabled: bool = False
_fit_files_dir: str | None = None


def configure(
    storage_backend: str,
    max_size_bytes: int,
    s3_bucket: str | None = None,
    s3_prefix: str = "raw-payloads",
    s3_endpoint_url: str | None = None,
    fit_files_enabled: bool = False,
    fit_files_dir: str | None = None,
    transport_enabled: bool = False,
) -> None:
    """Called once at startup from settings."""
    global _storage_backend, _max_size_bytes, _s3_bucket, _s3_prefix, _s3_client, _fit_files_enabled, _fit_files_dir
    _storage_backend = storage_backend
    _max_size_bytes = max_size_bytes
    _s3_prefix = s3_prefix
    _fit_files_enabled = False
    _fit_files_dir = None

    # FIT files to the local filesystem: no S3 needed.
    if fit_files_enabled and fit_files_dir:
        _fit_files_enabled = True
        _fit_files_dir = fit_files_dir

    # S3 client is required for the s3 raw-payload backend, for the SDK payload
    # transport, or for FIT files when no local directory was given (S3-backed
    # FIT storage).
    if storage_backend == "s3" or transport_enabled or (fit_files_enabled and not fit_files_dir):
        _s3_bucket = s3_bucket
        if not _s3_bucket:
            logger.error("S3 storage requested but no S3 bucket configured")
            if storage_backend == "s3":
                _storage_backend = "disabled"
            return
        _s3_client = _create_s3_client(endpoint_url=s3_endpoint_url)
        if _s3_client is None:
            logger.error("Failed to create S3 client - raw payload storage disabled")
            if storage_backend == "s3":
                _storage_backend = "disabled"
            return
        if storage_backend != "s3":
            # Client created solely for FIT file storage / payload transport
            _storage_backend = "disabled"
        if fit_files_enabled and not fit_files_dir:
            _fit_files_enabled = True


def _create_s3_client(endpoint_url: str | None = None) -> Any:
    """Create a boto3 S3 client using app AWS settings (shared factory)."""
    return create_s3_client(endpoint_url)


def store_raw_payload(
    *,
    source: str,
    provider: str,
    payload: Any,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    """Store a raw payload for debugging. No-op when disabled.

    Args:
        source: Origin type - "sdk", "webhook", or "api_response"
        provider: Provider name (e.g. "garmin", "apple", "strava")
        payload: Raw data (dict, list, or pre-serialized string)
        user_id: Optional user identifier for correlation
        trace_id: Optional trace/batch ID for correlation with processed data
    """
    if _storage_backend == "disabled":
        return

    payload_str = payload if isinstance(payload, str) else json.dumps(payload, default=json_serial)

    # Skip payloads that exceed size limit
    size = len(payload_str.encode("utf-8"))
    if size > _max_size_bytes:
        logger.warning(
            "Raw payload skipped (size %d bytes exceeds limit %d)",
            size,
            _max_size_bytes,
        )
        return

    if _storage_backend == "log":
        _store_to_log(source, provider, payload_str, size, user_id, trace_id)
    elif _storage_backend == "s3":
        put_payload_to_s3(source=source, provider=provider, payload=payload_str, user_id=user_id, trace_id=trace_id)


def get_payload_from_s3(ref: str) -> str:
    """Read a payload back from an ``s3://bucket/key`` reference.

    Raises on any read failure; the caller decides whether to retry the task.
    """
    if _s3_client is None:
        raise RuntimeError("Cannot get payload: S3 client not configured")
    bucket, key = _split_ref(ref)
    obj = _s3_client.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read().decode("utf-8")


def delete_payload_from_s3(ref: str) -> None:
    """Delete a payload by ``s3://bucket/key`` reference. Best-effort - never raises.

    Drops a transport-only copy once the data is persisted, so a deployment that opted out
    of payload archival does not accumulate them. Needs ``s3:DeleteObject``.
    """
    if _s3_client is None:
        return
    try:
        bucket, key = _split_ref(ref)
        _s3_client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        logger.exception("Failed to delete payload from S3: %s", ref)


def _split_ref(ref: str) -> tuple[str, str]:
    """Split ``s3://bucket/key``.

    The bucket travels in the reference because app/worker config drift would otherwise
    turn into a silent 404 on a payload that was written just fine.
    """
    if not ref.startswith("s3://"):
        # A bare key would split into a bucket named after our own prefix, reading from
        # someone else's bucket without a word of complaint.
        raise ValueError(f"Payload reference must be an s3:// URI: {ref}")
    bucket, _, key = ref[len("s3://") :].partition("/")
    if not bucket or not key:
        raise ValueError(f"Malformed S3 payload reference: {ref}")
    return bucket, key


def _store_to_log(
    source: str,
    provider: str,
    payload_str: str,
    size: int,
    user_id: str | None,
    trace_id: str | None,
) -> None:
    entry: dict[str, Any] = {
        "level": "debug",
        "message": "raw_payload",
        "source": source,
        "provider": provider,
        "size_bytes": size,
    }
    if user_id:
        entry["user_id"] = user_id
    if trace_id:
        entry["trace_id"] = trace_id
    entry["payload"] = payload_str

    print(json.dumps(entry), file=sys.stdout, flush=True)


def put_payload_to_s3(
    *,
    source: str,
    provider: str,
    payload: str,
    user_id: str | None = None,
    trace_id: str | None = None,
) -> str | None:
    """Upload a serialized payload to S3 and return its ``s3://bucket/key`` reference.

    Key format: {prefix}/{provider}/{source}/{YYYY-MM-DD}/{user_id}/{uuid}.json
    Metadata includes user_id, trace_id, and size for easy filtering.

    Returns None on failure - a caller that needs the reference has to handle that.
    """
    if _s3_client is None or _s3_bucket is None:
        logger.warning("S3 client or bucket not configured - skipping payload upload")
        return None

    body = payload.encode("utf-8")
    now = datetime.now(UTC)
    date_part = now.strftime("%Y-%m-%d")
    file_id = uuid4().hex[:12]
    user_part = user_id if user_id else "_unknown"
    key = f"{_s3_prefix}/{provider}/{source}/{date_part}/{user_part}/{file_id}.json"

    metadata: dict[str, str] = {
        "source": source,
        "provider": provider,
        "size_bytes": str(len(body)),
        "timestamp": now.isoformat(),
    }
    if user_id:
        metadata["user_id"] = user_id
    if trace_id:
        metadata["trace_id"] = trace_id

    try:
        _s3_client.put_object(
            Bucket=_s3_bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            Metadata=metadata,
        )
        logger.debug("Stored payload to S3: s3://%s/%s (%d bytes)", _s3_bucket, key, len(body))
        return f"s3://{_s3_bucket}/{key}"
    except Exception:
        logger.exception("Failed to store payload to S3: s3://%s/%s", _s3_bucket, key)
        return None


def _store_fit_file_local(rel_path: str, fit_bytes: bytes) -> bool:
    """Write a FIT file under the configured local directory. Returns True on success."""
    from pathlib import Path

    dest = Path(_fit_files_dir or "") / rel_path
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(fit_bytes)
        logger.debug("Stored FIT file locally: %s (%d bytes)", dest, len(fit_bytes))
        return True
    except Exception:
        log_structured(logger, "error", "Failed to store FIT file to disk", path=str(dest))
        return False


def store_fit_file(
    *,
    provider: str,
    fit_bytes: bytes,
    user_id: str,
    activity_id: str,
) -> str | None:
    """Store a raw FIT file. No-op (returns None) when STORE_FIT_FILES is disabled.

    Path: fit-files/{provider}/{YYYY-MM-DD}/{user_id}/{activity_id}.fit — written to
    the local FIT_FILES_DIR when set, otherwise uploaded to S3 (same client/bucket
    as raw payload storage). Returns the storage key on success (for later retrieval
    via get_fit_file), or None if storage is disabled or the write failed.
    """
    if not _fit_files_enabled:
        return None

    now = datetime.now(UTC)
    key = f"fit-files/{provider}/{now.strftime('%Y-%m-%d')}/{user_id}/{activity_id}.fit"

    if _fit_files_dir:
        return key if _store_fit_file_local(key, fit_bytes) else None

    if _s3_client is None or _s3_bucket is None:
        log_structured(logger, "warning", "Cannot store FIT file — S3 not configured")
        return None

    metadata = {"provider": provider, "user_id": user_id, "activity_id": str(activity_id)}

    try:
        _s3_client.put_object(
            Bucket=_s3_bucket,
            Key=key,
            Body=fit_bytes,
            ContentType="application/octet-stream",
            Metadata=metadata,
        )
        logger.debug("Stored FIT file to S3: s3://%s/%s (%d bytes)", _s3_bucket, key, len(fit_bytes))
    except Exception:
        log_structured(logger, "error", "Failed to store FIT file to S3", bucket=_s3_bucket, key=key)
        return None
    return key


def get_fit_file(key: str) -> bytes | None:
    """Read a stored FIT file by its storage key. Returns None if not found or unconfigured.

    Reads from the local FIT_FILES_DIR when configured, otherwise from S3 (same
    client/bucket used to store it).
    """
    from pathlib import Path

    if _fit_files_dir:
        dest = Path(_fit_files_dir) / key
        try:
            return dest.read_bytes()
        except FileNotFoundError:
            return None
        except Exception:
            log_structured(logger, "error", "Failed to read FIT file from disk", path=str(dest))
            return None

    if _s3_client is not None and _s3_bucket is not None:
        try:
            response = _s3_client.get_object(Bucket=_s3_bucket, Key=key)
            return response["Body"].read()
        except Exception:
            log_structured(logger, "error", "Failed to read FIT file from S3", bucket=_s3_bucket, key=key)
            return None

    return None
