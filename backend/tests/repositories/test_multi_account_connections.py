"""A user may hold several accounts with the same provider.

The constraint that used to enforce one-account-per-provider is gone; what
replaces it is narrower - the same *external* account cannot be linked twice to
one user - and the lookups that used to return "the" connection now have to say
which account they mean. These tests pin both, plus the scoping mechanism that
keeps a sync bound to one account.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.auth import ConnectionStatus
from app.schemas.enums import SdkConnectionOutcome
from app.utils.connection_context import active_connection
from tests.factories import UserConnectionFactory, UserFactory


@pytest.fixture
def repo() -> UserConnectionRepository:
    return UserConnectionRepository(UserConnection)


class TestSeveralAccountsPerProvider:
    def test_two_garmin_accounts_for_one_user_are_allowed(self, db: Session) -> None:
        """The headline case: one participant, two Garmins, worn at once."""
        user = UserFactory()

        left = UserConnectionFactory(
            user=user,
            provider="garmin",
            provider_user_id="garmin-left",
            account_label="P01 left wrist",
            account_email="p01.left@lab.example.edu",
        )
        right = UserConnectionFactory(
            user=user,
            provider="garmin",
            provider_user_id="garmin-right",
            account_label="P01 right wrist",
            account_email="p01.right@lab.example.edu",
        )
        db.commit()

        assert left.id != right.id
        assert {c.id for c in UserConnectionRepository().get_all_by_user_and_provider(db, user.id, "garmin")} == {
            left.id,
            right.id,
        }

    def test_the_same_external_account_cannot_be_linked_twice(self, db: Session) -> None:
        """Two rows for one Whoop login would double-count every sample."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="whoop", provider_user_id="whoop-1")
        db.commit()

        # The factory flushes on create, so the constraint fires there.
        with pytest.raises(IntegrityError):
            UserConnectionFactory(user=user, provider="whoop", provider_user_id="whoop-1")
        db.rollback()

    def test_the_same_account_email_cannot_be_linked_twice(self, db: Session) -> None:
        """The e-mail guard covers providers that report no user id, case-insensitively."""
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="polar",
            provider_user_id=None,
            account_email="P01@lab.example.edu",
        )
        db.commit()

        with pytest.raises(IntegrityError):
            UserConnectionFactory(
                user=user,
                provider="polar",
                provider_user_id=None,
                account_email="p01@lab.example.edu",
            )
        db.rollback()

    def test_a_revoked_account_does_not_block_reconnecting(self, db: Session) -> None:
        """Both uniqueness guards are limited to active rows on purpose."""
        user = UserFactory()
        UserConnectionFactory(
            user=user,
            provider="whoop",
            provider_user_id="whoop-1",
            status=ConnectionStatus.REVOKED,
        )
        db.commit()

        reconnected = UserConnectionFactory(user=user, provider="whoop", provider_user_id="whoop-1")
        db.commit()

        assert reconnected.status == ConnectionStatus.ACTIVE

    def test_two_users_may_share_one_external_account(self, db: Session) -> None:
        """Unchanged: the guards are per user, and linked profiles are a feature."""
        first, second = UserFactory(), UserFactory()
        UserConnectionFactory(user=first, provider="oura", provider_user_id="oura-shared")
        UserConnectionFactory(user=second, provider="oura", provider_user_id="oura-shared")
        db.commit()


class TestAmbiguousLookups:
    def test_unscoped_lookup_returns_the_oldest_account_deterministically(
        self, db: Session, repo: UserConnectionRepository
    ) -> None:
        """A caller that names no account gets a stable answer, not an arbitrary one."""
        user = UserFactory()
        now = datetime.now(timezone.utc)
        first = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id="g-1", created_at=now - timedelta(days=2)
        )
        UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-2", created_at=now)
        db.commit()

        for _ in range(3):
            assert repo.get_by_user_and_provider(db, user.id, "garmin").id == first.id

    def test_a_scope_selects_the_account_it_names(self, db: Session, repo: UserConnectionRepository) -> None:
        """This is what keeps a sync of the second watch off the first one's token."""
        user = UserFactory()
        now = datetime.now(timezone.utc)
        UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-1", created_at=now - timedelta(days=2))
        second = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-2", created_at=now)
        db.commit()

        with active_connection(second.id):
            assert repo.get_by_user_and_provider(db, user.id, "garmin").id == second.id
            assert repo.get_active_connection(db, user.id, "garmin").id == second.id

    def test_a_scope_naming_another_user_is_ignored(self, db: Session, repo: UserConnectionRepository) -> None:
        """A mismatched scope must not answer a question it was not asked."""
        first, second = UserFactory(), UserFactory()
        theirs = UserConnectionFactory(user=second, provider="garmin", provider_user_id="g-other")
        mine = UserConnectionFactory(user=first, provider="garmin", provider_user_id="g-mine")
        db.commit()

        with active_connection(theirs.id):
            assert repo.get_by_user_and_provider(db, first.id, "garmin").id == mine.id

    def test_a_scope_naming_a_revoked_account_reports_no_active_connection(
        self, db: Session, repo: UserConnectionRepository
    ) -> None:
        user = UserFactory()
        revoked = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id="g-1", status=ConnectionStatus.REVOKED
        )
        UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-2")
        db.commit()

        with active_connection(revoked.id):
            assert repo.get_active_connection(db, user.id, "garmin") is None

    def test_the_scope_does_not_outlive_its_block(self, db: Session, repo: UserConnectionRepository) -> None:
        """A leaked scope would bind the next task on this worker thread."""
        user = UserFactory()
        now = datetime.now(timezone.utc)
        first = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id="g-1", created_at=now - timedelta(days=2)
        )
        second = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-2", created_at=now)
        db.commit()

        with active_connection(second.id):
            pass
        assert repo.get_by_user_and_provider(db, user.id, "garmin").id == first.id


class TestScopedOperations:
    def test_get_by_id_for_user_will_not_cross_users(self, db: Session, repo: UserConnectionRepository) -> None:
        """A connection id from one participant must never address another's account."""
        mine, theirs = UserFactory(), UserFactory()
        connection = UserConnectionFactory(user=theirs, provider="garmin", provider_user_id="g-other")
        db.commit()

        assert repo.get_by_id_for_user(db, mine.id, connection.id) is None
        assert repo.get_by_id_for_user(db, theirs.id, connection.id).id == connection.id

    def test_disconnect_one_account_leaves_the_comparator_connected(
        self, db: Session, repo: UserConnectionRepository
    ) -> None:
        """Dropping one arm of a study must not take the device worn beside it."""
        user = UserFactory()
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right")
        db.commit()

        assert repo.disconnect(db, user.id, "garmin", left.id) == 1
        db.refresh(left)
        db.refresh(right)

        assert left.status == ConnectionStatus.REVOKED
        assert left.access_token is None
        assert right.status == ConnectionStatus.ACTIVE
        assert right.access_token is not None

    def test_disconnect_without_an_account_revokes_every_account(
        self, db: Session, repo: UserConnectionRepository
    ) -> None:
        """ "Disconnect me from Garmin" still means all of it."""
        user = UserFactory()
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-left")
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id="g-right")
        db.commit()

        assert repo.disconnect(db, user.id, "garmin") == 2
        db.refresh(left)
        db.refresh(right)
        assert left.status == ConnectionStatus.REVOKED
        assert right.status == ConnectionStatus.REVOKED

    def test_get_by_account_email_is_case_insensitive(self, db: Session, repo: UserConnectionRepository) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user, provider="whoop", provider_user_id="w-1", account_email="P01.Left@Lab.Example.Edu"
        )
        db.commit()

        found = repo.get_by_account_email(db, user.id, "whoop", "  p01.left@lab.example.edu ")
        assert found is not None
        assert found.id == connection.id

    def test_update_account_metadata_clears_only_what_it_is_told_to(
        self, db: Session, repo: UserConnectionRepository
    ) -> None:
        """A rename must not be able to drop the e-mail that records provenance."""
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            provider_user_id="w-1",
            account_label="old",
            account_email="p01@lab.example.edu",
        )
        db.commit()

        repo.update_account_metadata(db, connection, account_label="P01 chest")
        assert connection.account_label == "P01 chest"
        assert connection.account_email == "p01@lab.example.edu"

        repo.update_account_metadata(db, connection, clear_account_email=True)
        assert connection.account_email is None
        assert connection.account_label == "P01 chest"


class TestSdkAccounts:
    def test_an_sdk_account_is_addressed_by_email(self, db: Session, repo: UserConnectionRepository) -> None:
        """Two phones on two Apple IDs are two accounts, not one."""
        user = UserFactory()

        first, _ = repo.ensure_sdk_connection(db, user.id, "apple", account_email="phone-a@lab.example.edu")
        second, _ = repo.ensure_sdk_connection(db, user.id, "apple", account_email="phone-b@lab.example.edu")
        again, outcome = repo.ensure_sdk_connection(db, user.id, "apple", account_email="phone-a@lab.example.edu")

        assert first.id != second.id
        assert again.id == first.id
        assert outcome == SdkConnectionOutcome.EXISTING

    def test_without_an_email_the_behaviour_is_unchanged(self, db: Session, repo: UserConnectionRepository) -> None:
        user = UserFactory()
        first, _ = repo.ensure_sdk_connection(db, user.id, "apple")
        second, outcome = repo.ensure_sdk_connection(db, user.id, "apple")

        assert first.id == second.id
        assert outcome == SdkConnectionOutcome.EXISTING


class TestDisplayLabel:
    def test_an_unnamed_account_still_reads_as_something(self, db: Session) -> None:
        """Nothing in a connection list should ever render blank."""
        from app.schemas.model_crud.user_management import UserConnectionRead

        user = UserFactory()
        connection = UserConnectionFactory(
            user=user, provider="polar", provider_user_id="p-1", provider_username=None, account_email=None
        )
        db.commit()

        read = UserConnectionRead.model_validate(connection)
        assert read.display_label.startswith("polar (")

    def test_the_label_wins_over_everything_else(self, db: Session) -> None:
        from app.schemas.model_crud.user_management import UserConnectionRead

        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            provider_user_id="w-1",
            provider_username="someone",
            account_label="P01 chest",
            account_email="p01@lab.example.edu",
        )
        db.commit()

        assert UserConnectionRead.model_validate(connection).display_label == "P01 chest"
