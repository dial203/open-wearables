"""Normalise the per-record metadata a mobile SDK payload carries.

The SDK schema types ``metadata`` as ``list[dict] | dict | None`` because the two
shapes both occur: HealthKit hands the iOS SDK a dictionary, while Apple's XML export
writes a sequence of ``<MetadataEntry key=... value=.../>`` elements. Both describe the
same thing, and storing them in two shapes would push the difference onto every reader.

Nothing here interprets a key. The point is only to get one predictable container into
the column so the real distribution of keys can be observed from stored data; typing any
particular key comes after that, and from evidence.
"""

from typing import Any

# A list entry that names its own key, as the Apple XML export writes it.
_KEY = "key"
_VALUE = "value"


def normalize_record_metadata(raw: list[dict[str, Any]] | dict[str, Any] | None) -> dict[str, Any] | None:
    """A single dict for a record's metadata, or None when it carries none.

    Empty is None rather than ``{}``: a writer that sent nothing and a writer that sent
    an empty container are the same statement, and collapsing them keeps the column NULL
    on the rows - the large majority, across the whole table - that say nothing at all.

    A list of ``{"key": ..., "value": ...}`` entries is folded into the dict it
    describes. Any other list is kept verbatim under ``entries``, because a shape nobody
    has seen yet is not one to guess at, and losing it here would repeat the mistake this
    module exists to undo.
    """
    if raw is None:
        return None

    if isinstance(raw, dict):
        return dict(raw) or None

    entries = [item for item in raw if isinstance(item, dict)]
    if not entries:
        return None

    if all(_KEY in item for item in entries):
        folded = {str(item[_KEY]): item.get(_VALUE) for item in entries}
        return folded or None

    return {"entries": entries}
