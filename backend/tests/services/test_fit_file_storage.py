"""Tests for local-filesystem FIT file storage."""

from pathlib import Path

from app.services import raw_payload_storage


def _reset() -> None:
    raw_payload_storage.configure(storage_backend="disabled", max_size_bytes=10 * 1024 * 1024)


def test_store_fit_file_local(tmp_path: Path) -> None:
    raw_payload_storage.configure(
        storage_backend="disabled",
        max_size_bytes=10 * 1024 * 1024,
        fit_files_enabled=True,
        fit_files_dir=str(tmp_path),
    )
    try:
        key = raw_payload_storage.store_fit_file(
            provider="garmin", fit_bytes=b"FITDATA", user_id="user-1", activity_id="act-1"
        )
        # store returns the storage key on success
        assert key is not None
        assert key.startswith("fit-files/garmin/")
        assert key.endswith("/user-1/act-1.fit")
        files = list(tmp_path.rglob("*.fit"))
        assert len(files) == 1
        assert files[0].name == "act-1.fit"
        assert files[0].read_bytes() == b"FITDATA"
        # laid out as fit-files/{provider}/{date}/{user}/{activity}.fit
        assert "fit-files/garmin" in files[0].as_posix()
        assert "user-1" in files[0].as_posix()
    finally:
        _reset()


def test_get_fit_file_roundtrip(tmp_path: Path) -> None:
    raw_payload_storage.configure(
        storage_backend="disabled",
        max_size_bytes=10 * 1024 * 1024,
        fit_files_enabled=True,
        fit_files_dir=str(tmp_path),
    )
    try:
        key = raw_payload_storage.store_fit_file(
            provider="garmin", fit_bytes=b"ROUNDTRIP", user_id="u", activity_id="a"
        )
        assert key is not None
        assert raw_payload_storage.get_fit_file(key) == b"ROUNDTRIP"
        # unknown key returns None, not an error
        assert raw_payload_storage.get_fit_file("fit-files/garmin/2026-01-01/u/missing.fit") is None
    finally:
        _reset()


def test_store_fit_file_disabled_is_noop(tmp_path: Path) -> None:
    raw_payload_storage.configure(storage_backend="disabled", max_size_bytes=10 * 1024 * 1024)
    key = raw_payload_storage.store_fit_file(provider="garmin", fit_bytes=b"X", user_id="u", activity_id="a")
    assert key is None
    assert list(tmp_path.rglob("*.fit")) == []


def test_get_fit_file_disabled_returns_none() -> None:
    raw_payload_storage.configure(storage_backend="disabled", max_size_bytes=10 * 1024 * 1024)
    assert raw_payload_storage.get_fit_file("fit-files/garmin/2026-01-01/u/a.fit") is None
