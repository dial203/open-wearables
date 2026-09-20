"""Data sources are per connected account, not per provider.

Two accounts with one provider report the same device_model and the same
source, so the identity that keyed a data source before - (user, provider,
device_model, source) - no longer distinguishes two units worn at the same
time. If it did not include the connection, both wrists' samples would land on
one data_source and could not be separated again afterwards, which is the one
failure mode in this feature that is not recoverable.
"""

import pytest
from sqlalchemy.orm import Session

from app.models import DataSource
from app.repositories.data_source_repository import DataSourceRepository
from app.schemas.enums import ProviderName
from app.utils.connection_context import active_connection
from tests.factories import UserConnectionFactory, UserFactory


@pytest.fixture
def repo() -> DataSourceRepository:
    return DataSourceRepository(DataSource)


class TestIdentityIncludesTheAccount:
    def test_two_accounts_reporting_the_same_device_get_separate_sources(
        self, db: Session, repo: DataSourceRepository
    ) -> None:
        """The whole point: two identical fenix watches must not pool."""
        user = UserFactory()
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right")
        db.commit()

        left_source = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.GARMIN,
            user_connection_id=left.id,
            device_model="fenix 7",
        )
        right_source = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.GARMIN,
            user_connection_id=right.id,
            device_model="fenix 7",
        )
        db.commit()

        assert left_source.id != right_source.id
        assert left_source.user_connection_id == left.id
        assert right_source.user_connection_id == right.id

    def test_the_same_account_reuses_its_source(self, db: Session, repo: DataSourceRepository) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-1")
        db.commit()

        first = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.GARMIN,
            user_connection_id=connection.id,
            device_model="fenix 7",
        )
        second = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.GARMIN,
            user_connection_id=connection.id,
            device_model="fenix 7",
        )
        db.commit()

        assert first.id == second.id

    def test_the_active_scope_supplies_the_account_when_the_caller_does_not(
        self, db: Session, repo: DataSourceRepository
    ) -> None:
        """Most provider code never learned to pass a connection id."""
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider="oura", provider_user_id="o-1")
        db.commit()

        with active_connection(connection.id):
            source = repo.ensure_data_source(db, user_id=user.id, provider=ProviderName.OURA)
        db.commit()

        assert source.user_connection_id == connection.id

    def test_a_one_time_import_stays_connection_less(self, db: Session, repo: DataSourceRepository) -> None:
        """An XML upload runs in no scope and belongs to no account."""
        user = UserFactory()

        source = repo.ensure_data_source(db, user_id=user.id, provider=ProviderName.APPLE, device_model="Watch7,5")
        db.commit()

        assert source.user_connection_id is None


class TestOrphanAdoption:
    def test_a_single_account_adopts_a_connection_less_source(self, db: Session, repo: DataSourceRepository) -> None:
        """History predating the connection joins it rather than forking beside it."""
        user = UserFactory()
        orphan = repo.ensure_data_source(db, user_id=user.id, provider=ProviderName.OURA, device_model="Oura Gen3")
        db.commit()

        connection = UserConnectionFactory(user=user, provider="oura", provider_user_id="o-1")
        db.commit()

        adopted = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.OURA,
            user_connection_id=connection.id,
            device_model="Oura Gen3",
        )
        db.commit()

        assert adopted.id == orphan.id
        assert adopted.user_connection_id == connection.id

    def test_two_accounts_never_adopt_an_orphan(self, db: Session, repo: DataSourceRepository) -> None:
        """With two rings there is no way to know whose history this was.

        A wrong split is visible and reversible; a wrong merge pools two units'
        samples and cannot be undone, so the ambiguous case forks deliberately.
        """
        user = UserFactory()
        orphan = repo.ensure_data_source(db, user_id=user.id, provider=ProviderName.OURA, device_model="Oura Gen3")
        db.commit()

        first = UserConnectionFactory(user=user, provider="oura", provider_user_id="o-1")
        UserConnectionFactory(user=user, provider="oura", provider_user_id="o-2")
        db.commit()

        created = repo.ensure_data_source(
            db,
            user_id=user.id,
            provider=ProviderName.OURA,
            user_connection_id=first.id,
            device_model="Oura Gen3",
        )
        db.commit()

        assert created.id != orphan.id
        assert orphan.user_connection_id is None


class TestDeviceLabels:
    def test_each_account_gets_its_own_device_label(self, db: Session, repo: DataSourceRepository) -> None:
        """Whoop reports no device, so the label is the only device information there is."""
        user = UserFactory()
        left = UserConnectionFactory(
            user=user, provider="whoop", provider_user_id="w-left", device_label="Whoop 5.0 (left)"
        )
        right = UserConnectionFactory(
            user=user, provider="whoop", provider_user_id="w-right", device_label="Whoop 4.0 (right)"
        )
        db.commit()

        left_source = repo.ensure_data_source(
            db, user_id=user.id, provider=ProviderName.WHOOP, user_connection_id=left.id
        )
        right_source = repo.ensure_data_source(
            db, user_id=user.id, provider=ProviderName.WHOOP, user_connection_id=right.id
        )
        db.commit()

        assert left_source.device_model == "Whoop 5.0 (left)"
        assert right_source.device_model == "Whoop 4.0 (right)"

    def test_relabelling_one_account_leaves_the_other_alone(self, db: Session, repo: DataSourceRepository) -> None:
        user = UserFactory()
        left = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-left")
        right = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-right")
        db.commit()

        left_source = repo.ensure_data_source(
            db, user_id=user.id, provider=ProviderName.WHOOP, user_connection_id=left.id
        )
        right_source = repo.ensure_data_source(
            db, user_id=user.id, provider=ProviderName.WHOOP, user_connection_id=right.id
        )
        db.commit()

        repo.set_connection_device_label(db, user.id, ProviderName.WHOOP, "Whoop 5.0 (left)", left.id)
        db.commit()
        db.refresh(left_source)
        db.refresh(right_source)

        assert left_source.device_model == "Whoop 5.0 (left)"
        assert right_source.device_model is None


class TestPurgingOneAccount:
    def test_purging_one_account_leaves_the_comparators_data(self, db: Session, repo: DataSourceRepository) -> None:
        """Dropping one arm of a study must not take the device worn beside it."""
        user = UserFactory()
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right")
        db.commit()

        repo.ensure_data_source(
            db, user_id=user.id, provider=ProviderName.GARMIN, user_connection_id=left.id, device_model="fenix 7"
        )
        right_source = repo.ensure_data_source(
            db, user_id=user.id, provider=ProviderName.GARMIN, user_connection_id=right.id, device_model="fenix 7"
        )
        db.commit()

        assert repo.delete_connection_data(db, user.id, left.id) == 1

        remaining = repo.get_user_data_sources(db, user.id)
        assert [source.id for source in remaining] == [right_source.id]
