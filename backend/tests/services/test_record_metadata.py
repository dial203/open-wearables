"""Normalising the per-record metadata a payload carries (pure, no DB)."""

from app.services.sdk.record_metadata import normalize_record_metadata


class TestNormalizeRecordMetadata:
    def test_a_dict_passes_through(self) -> None:
        raw = {"HKMetadataKeyHeartRateSensorLocation": "6", "HKMetadataKeyHeartRateMotionContext": "1"}
        assert normalize_record_metadata(raw) == raw

    def test_the_result_is_a_copy(self) -> None:
        """The column must not alias a dict the caller still holds."""
        raw = {"a": "1"}
        out = normalize_record_metadata(raw)
        assert out is not raw

    def test_key_value_entries_are_folded(self) -> None:
        """Apple's XML export writes a sequence of MetadataEntry elements."""
        raw = [
            {"key": "HKMetadataKeyHeartRateSensorLocation", "value": "6"},
            {"key": "HKMetadataKeyWasUserEntered", "value": "0"},
        ]
        assert normalize_record_metadata(raw) == {
            "HKMetadataKeyHeartRateSensorLocation": "6",
            "HKMetadataKeyWasUserEntered": "0",
        }

    def test_an_unrecognised_list_shape_is_kept_verbatim(self) -> None:
        """A shape nobody has seen is not one to guess at - and dropping it here would
        repeat the mistake the column exists to undo."""
        raw = [{"sensor": "chest"}, {"sensor": "wrist"}]
        assert normalize_record_metadata(raw) == {"entries": raw}

    def test_nothing_sent_is_none(self) -> None:
        assert normalize_record_metadata(None) is None

    def test_empty_containers_are_none_not_empty(self) -> None:
        """A writer that sent nothing and one that sent an empty container say the same
        thing, and collapsing them keeps the column NULL on the rows that say nothing."""
        assert normalize_record_metadata({}) is None
        assert normalize_record_metadata([]) is None
        assert normalize_record_metadata([1, 2]) is None  # ty:ignore[invalid-argument-type]

    def test_a_missing_value_is_kept_as_none(self) -> None:
        assert normalize_record_metadata([{"key": "k"}]) == {"k": None}
