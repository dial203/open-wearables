"""Bulk data-source resolution fills device_model from the connection label.

Time series arrive through batch_ensure_data_sources while events go through
ensure_data_source. Only the latter used to fill device_model from the connection's
device_label, so a provider that reports no device (Oura, Whoop) ended up with its
time series on a device-less source and its events on the labelled one.

The subtle part is the return mapping: callers look rows up by the identity they
passed in (device_model=None), so the result must stay keyed by that even though the
row actually stored carries the label. Getting this wrong silently drops samples.
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

from app.models import DataSource
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import ProviderName


class _FakeQuery:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *_args: object, **_kwargs: object) -> "_FakeQuery":
        return self

    def all(self) -> list:
        return self._rows


class _FakeSession:
    """Returns each queued row-set in turn (lookup, then post-insert re-query)."""

    def __init__(self, *row_sets: list) -> None:
        self._row_sets = list(row_sets)
        self.executed: list = []

    def query(self, _model: object) -> _FakeQuery:
        rows = self._row_sets.pop(0) if self._row_sets else []
        return _FakeQuery(rows)

    def execute(self, stmt: object) -> None:
        self.executed.append(stmt)

    def flush(self) -> None:
        pass


def _repo(label: str | None) -> DataSourceRepository:
    repo = DataSourceRepository()
    repo._connection_device_label = lambda *_a, **_k: label  # type: ignore[method-assign]
    return repo


def _row(user_id: UUID, device_model: str | None, source: str | None, ds_id: UUID) -> SimpleNamespace:
    return SimpleNamespace(user_id=user_id, device_model=device_model, source=source, id=ds_id)


def test_null_device_request_resolves_to_the_labelled_source() -> None:
    """The bug: an Oura time series (device_model=None) must reuse the labelled row."""
    user_id, ds_id = uuid4(), uuid4()
    existing = [_row(user_id, "Oura Ring Or5", "oura", ds_id)]
    session = _FakeSession(existing)
    repo = _repo("Oura Ring Or5")

    result = repo.batch_ensure_data_sources(
        session,  # type: ignore[arg-type]
        ProviderName.OURA,
        None,
        {(user_id, None, "oura")},
    )

    # keyed by what the caller asked for, resolving to the labelled source
    assert result == {(user_id, None, "oura"): ds_id}
    # and no new row was inserted — the existing one was reused
    assert session.executed == []


def test_no_connection_label_leaves_device_model_null() -> None:
    """With no label to apply, behaviour is unchanged."""
    user_id, ds_id = uuid4(), uuid4()
    session = _FakeSession([], [_row(user_id, None, "oura", ds_id)])
    repo = _repo(None)

    result = repo.batch_ensure_data_sources(
        session,  # type: ignore[arg-type]
        ProviderName.OURA,
        None,
        {(user_id, None, "oura")},
    )

    assert result == {(user_id, None, "oura"): ds_id}
    assert len(session.executed) == 1  # a row had to be created


def test_explicit_device_model_is_never_overridden() -> None:
    """A provider that reports its own device keeps it, label or not."""
    user_id, ds_id = uuid4(), uuid4()
    existing = [_row(user_id, "Forerunner 965", "garmin", ds_id)]
    session = _FakeSession(existing)
    repo = _repo("Some Other Device")

    result = repo.batch_ensure_data_sources(
        session,  # type: ignore[arg-type]
        ProviderName.GARMIN,
        None,
        {(user_id, "Forerunner 965", "garmin")},
    )

    assert result == {(user_id, "Forerunner 965", "garmin"): ds_id}
    assert session.executed == []


def test_new_row_is_created_with_the_label_applied() -> None:
    """Nothing exists yet: the inserted row must carry the label, not null."""
    user_id, ds_id = uuid4(), uuid4()
    # lookup finds nothing; the post-insert re-query returns the labelled row
    session = _FakeSession([], [_row(user_id, "Whoop 5.0", "whoop", ds_id)])
    repo = _repo("Whoop 5.0")

    result = repo.batch_ensure_data_sources(
        session,  # type: ignore[arg-type]
        ProviderName.WHOOP,
        None,
        {(user_id, None, "whoop")},
    )

    assert result == {(user_id, None, "whoop"): ds_id}
    assert len(session.executed) == 1
    inserted = session.executed[0].compile().params
    assert inserted["device_model_m0"] == "Whoop 5.0"


def test_model_attribute_is_the_data_source_model() -> None:
    """Guards the _FakeSession stand-in against a signature drift."""
    assert DataSourceRepository().model is DataSource
