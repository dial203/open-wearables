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
        raw_payload_storage.store_fit_file(
            provider="garmin", fit_bytes=b"FITDATA", user_id="user-1", activity_id="act-1"
        )
        files = list(tmp_path.rglob("*.fit"))
        assert len(files) == 1
        assert files[0].name == "act-1.fit"
        assert files[0].read_bytes() == b"FITDATA"
        # laid out as fit-files/{provider}/{date}/{user}/{activity}.fit
        assert "fit-files/garmin" in files[0].as_posix()
        assert "user-1" in files[0].as_posix()
    finally:
        _reset()


def test_store_fit_file_disabled_is_noop(tmp_path: Path) -> None:
    raw_payload_storage.configure(storage_backend="disabled", max_size_bytes=10 * 1024 * 1024)
    raw_payload_storage.store_fit_file(provider="garmin", fit_bytes=b"X", user_id="u", activity_id="a")
    assert list(tmp_path.rglob("*.fit")) == []
