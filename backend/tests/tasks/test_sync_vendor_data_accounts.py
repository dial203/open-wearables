"""The pull sync walks a user's accounts one at a time.

A participant wearing two Garmins has two connections, two tokens and two sets
of data sources. The task iterated connections already; what is new is that it
binds each one for the duration of its iteration, so the provider code behind it
- all of which resolves credentials from (user, provider) - acts for the right
account, and that the result no longer collapses two accounts onto one key.
"""

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.integrations.celery.tasks.sync_vendor_data_task import sync_vendor_data
from app.schemas.auth import ConnectionStatus
from app.utils.connection_context import get_active_connection_id
from tests.factories import UserConnectionFactory, UserFactory


def _pull_strategy(seen: list | None = None) -> MagicMock:
    """A rest_pull provider whose workout load records the bound account."""
    workouts = MagicMock()

    def load_data(*_args: object, **_kwargs: object) -> bool:
        if seen is not None:
            seen.append(get_active_connection_id())
        return True

    workouts.load_data.side_effect = load_data

    strategy = MagicMock()
    strategy.capabilities.rest_pull = True
    strategy.capabilities.webhook_stream = False
    strategy.capabilities.max_historical_days = None
    strategy.workouts = workouts
    strategy.data_247 = None
    return strategy


class TestTwoAccountsWithOneProvider:
    @patch("app.integrations.celery.tasks.sync_vendor_data_task.SessionLocal")
    @patch("app.services.providers.factory.ProviderFactory.get_provider")
    def test_each_account_is_synced_under_its_own_scope(
        self,
        mock_get_provider: MagicMock,
        mock_session_local: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        user = UserFactory()
        left = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id=None, status=ConnectionStatus.ACTIVE
        )
        right = UserConnectionFactory(
            user=user, provider="garmin", provider_user_id=None, status=ConnectionStatus.ACTIVE
        )
        db.commit()

        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = None
        seen: list = []
        mock_get_provider.return_value = _pull_strategy(seen)

        result = sync_vendor_data(str(user.id))

        assert set(seen) == {left.id, right.id}
        assert len(result["providers_synced"]) == 2
        assert result["errors"] == {}

    @patch("app.integrations.celery.tasks.sync_vendor_data_task.SessionLocal")
    @patch("app.services.providers.factory.ProviderFactory.get_provider")
    def test_the_result_names_each_account(
        self,
        mock_get_provider: MagicMock,
        mock_session_local: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Two accounts under one "garmin" key would mean one silently overwrote the other."""
        user = UserFactory()
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id=None)
        right = UserConnectionFactory(user=user, provider="garmin", provider_user_id=None)
        db.commit()

        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = None
        mock_get_provider.return_value = _pull_strategy()

        result = sync_vendor_data(str(user.id))

        assert set(result["providers_synced"]) == {f"garmin:{left.id}", f"garmin:{right.id}"}
        assert {entry["params"]["connection_id"] for entry in result["providers_synced"].values()} == {
            str(left.id),
            str(right.id),
        }

    @patch("app.integrations.celery.tasks.sync_vendor_data_task.SessionLocal")
    @patch("app.services.providers.factory.ProviderFactory.get_provider")
    def test_a_single_account_keeps_the_plain_provider_key(
        self,
        mock_get_provider: MagicMock,
        mock_session_local: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """Existing consumers read result["providers_synced"]["garmin"]."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", provider_user_id=None)
        db.commit()

        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = None
        mock_get_provider.return_value = _pull_strategy()

        result = sync_vendor_data(str(user.id))

        assert list(result["providers_synced"]) == ["garmin"]

    @patch("app.integrations.celery.tasks.sync_vendor_data_task.SessionLocal")
    @patch("app.services.providers.factory.ProviderFactory.get_provider")
    def test_connection_ids_restrict_the_run_to_one_account(
        self,
        mock_get_provider: MagicMock,
        mock_session_local: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """A freshly connected account is backfilled without re-pulling its siblings."""
        user = UserFactory()
        left = UserConnectionFactory(user=user, provider="garmin", provider_user_id=None)
        UserConnectionFactory(user=user, provider="garmin", provider_user_id=None)
        db.commit()

        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = None
        seen: list = []
        mock_get_provider.return_value = _pull_strategy(seen)

        sync_vendor_data(str(user.id), connection_ids=[str(left.id)])

        assert seen == [left.id]

    @patch("app.integrations.celery.tasks.sync_vendor_data_task.SessionLocal")
    @patch("app.services.providers.factory.ProviderFactory.get_provider")
    def test_the_scope_is_cleared_when_the_task_ends(
        self,
        mock_get_provider: MagicMock,
        mock_session_local: MagicMock,
        db: Session,
        mock_celery_app: MagicMock,
    ) -> None:
        """A Celery worker reuses threads; a leaked scope would bind the next task."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="garmin", provider_user_id=None)
        db.commit()

        mock_session_local.return_value.__enter__.return_value = db
        mock_session_local.return_value.__exit__.return_value = None
        mock_get_provider.return_value = _pull_strategy()

        sync_vendor_data(str(user.id))

        assert get_active_connection_id() is None
