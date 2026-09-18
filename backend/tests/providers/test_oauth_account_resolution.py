"""Which account an OAuth callback lands on.

The provider's callback carries only a code and the state we put there, so
whether the person is re-authorising an account they already hold or adding
another one beside it has to be decided when the flow starts and carried
through. These tests pin the resolution order, because getting it wrong either
silently overwrites the tokens of the wrong wearable or produces a duplicate of
an account already linked - and both are only noticed once the data is wrong.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.model_crud.credentials import (
    OAuthState,
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from tests.factories import UserConnectionFactory, UserFactory


class _Template(BaseOAuthTemplate):
    """The template with only its abstract endpoints filled in."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        raise NotImplementedError

    @property
    def credentials(self) -> ProviderCredentials:
        raise NotImplementedError


@pytest.fixture
def template() -> _Template:
    return _Template(
        user_repo=None,  # type: ignore[arg-type]
        connection_repo=UserConnectionRepository(),
        provider_name="whoop",
        api_base_url="https://api.prod.whoop.com",
    )


def _state(user_id: UUID, **kwargs: object) -> OAuthState:
    return OAuthState(user_id=user_id, provider="whoop", **kwargs)


def _tokens() -> OAuthTokenResponse:
    return OAuthTokenResponse(
        access_token="new-access",
        token_type="bearer",
        refresh_token="new-refresh",
        expires_in=3600,
    )


class TestResolution:
    def test_an_explicit_connection_id_wins(self, db: Session, template: _Template) -> None:
        user = UserFactory()
        first = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        second = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-2")
        db.commit()

        resolved = template._resolve_target_connection(
            db, user.id, None, None, _state(user.id, connection_id=second.id)
        )
        assert resolved.id == second.id
        assert resolved.id != first.id

    def test_a_connection_id_for_another_user_is_refused(self, db: Session, template: _Template) -> None:
        """Falling back to some other account would re-point a reconnect at the wrong wearable."""
        mine, theirs = UserFactory(), UserFactory()
        UserConnectionFactory(user=mine, provider="whoop", provider_user_id="w-mine")
        theirs_connection = UserConnectionFactory(user=theirs, provider="whoop", provider_user_id="w-theirs")
        db.commit()

        with pytest.raises(HTTPException) as exc:
            template._resolve_target_connection(
                db, mine.id, None, None, _state(mine.id, connection_id=theirs_connection.id)
            )
        assert exc.value.status_code == 400

    def test_a_missing_connection_id_is_refused(self, db: Session, template: _Template) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        db.commit()

        with pytest.raises(HTTPException):
            template._resolve_target_connection(db, user.id, None, None, _state(user.id, connection_id=uuid4()))

    def test_the_provider_user_id_matches_an_account_we_already_hold(self, db: Session, template: _Template) -> None:
        """ "Add another" must not duplicate an account the person already linked."""
        user = UserFactory()
        UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        second = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-2")
        db.commit()

        resolved = template._resolve_target_connection(db, user.id, "w-2", None, _state(user.id, new_account=True))
        assert resolved.id == second.id

    def test_an_unrecognised_provider_user_id_creates_an_account_without_the_flag(
        self, db: Session, template: _Template
    ) -> None:
        """The participant-facing pairing page cannot set new_account.

        It is unauthenticated, so it cannot read the connection list and cannot
        know this is the second Whoop. The provider naming an account we do not
        hold is enough on its own - without this the second account's tokens
        would overwrite the first's and both wrists would report as one.
        """
        user = UserFactory()
        first = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        db.commit()

        resolved = template._resolve_target_connection(db, user.id, "w-2", None, _state(user.id))

        assert resolved is None, f"would have re-pointed {first.id} at a different Whoop account"

    def test_a_recognised_provider_user_id_still_reauthorizes_that_account(
        self, db: Session, template: _Template
    ) -> None:
        """The other half of the same rule: signing back in is not a new account."""
        user = UserFactory()
        first = UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-2")
        db.commit()

        assert template._resolve_target_connection(db, user.id, "w-1", None, _state(user.id)).id == first.id

    def test_an_account_with_no_recorded_provider_user_id_stays_ambiguous(
        self, db: Session, template: _Template
    ) -> None:
        """A connection that predates us learning its id may well be this one.

        Creating a second row would strand the first; adopting it is the
        pre-existing behaviour and the recoverable choice.
        """
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider="whoop", provider_user_id=None)
        db.commit()

        assert template._resolve_target_connection(db, user.id, "w-1", None, _state(user.id)).id == connection.id

    def test_the_account_email_matches_when_the_provider_reports_no_user_id(
        self, db: Session, template: _Template
    ) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user, provider="whoop", provider_user_id=None, account_email="p01@lab.example.edu"
        )
        db.commit()

        resolved = template._resolve_target_connection(db, user.id, None, "P01@lab.example.edu", _state(user.id))
        assert resolved.id == connection.id

    def test_new_account_creates_one_when_nothing_matches(self, db: Session, template: _Template) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="whoop", provider_user_id="w-1")
        db.commit()

        assert (
            template._resolve_target_connection(db, user.id, "w-new", None, _state(user.id, new_account=True)) is None
        )

    def test_a_single_existing_account_is_reauthorized_by_default(self, db: Session, template: _Template) -> None:
        """The pre-multi-account path: an ordinary reconnect still works untouched."""
        user = UserFactory()
        connection = UserConnectionFactory(user=user, provider="whoop", provider_user_id=None)
        db.commit()

        assert template._resolve_target_connection(db, user.id, None, None, _state(user.id)).id == connection.id

    def test_an_unidentifiable_callback_with_several_accounts_creates_a_new_one(
        self, db: Session, template: _Template
    ) -> None:
        """Rather than overwrite one of two accounts at random.

        A spurious third row is visible in the connection list and can be
        disconnected; silently re-pointing one of two wearables' tokens is not.
        """
        user = UserFactory()
        UserConnectionFactory(user=user, provider="whoop", provider_user_id=None, account_email="a@lab.edu")
        UserConnectionFactory(user=user, provider="whoop", provider_user_id=None, account_email="b@lab.edu")
        db.commit()

        assert template._resolve_target_connection(db, user.id, None, None, _state(user.id)) is None


class TestPersistedAccountFields:
    def test_a_new_account_records_its_label_and_email(self, db: Session, template: _Template) -> None:
        user = UserFactory()

        template._save_connection(
            db,
            user.id,
            _tokens(),
            {"user_id": "w-1", "username": None, "email": "p01.left@lab.example.edu"},
            _state(user.id, new_account=True, account_label="P01 left wrist"),
        )

        connection = UserConnectionRepository().get_by_user_and_provider(db, user.id, "whoop")
        assert connection.account_label == "P01 left wrist"
        assert connection.account_email == "p01.left@lab.example.edu"

    def test_the_provider_email_wins_over_the_typed_one(self, db: Session, template: _Template) -> None:
        """It is the authoritative record of which login the data came from."""
        user = UserFactory()

        template._save_connection(
            db,
            user.id,
            _tokens(),
            {"user_id": "w-1", "username": None, "email": "real@lab.example.edu"},
            _state(user.id, new_account=True, account_email="typo@lab.example.edu"),
        )

        connection = UserConnectionRepository().get_by_user_and_provider(db, user.id, "whoop")
        assert connection.account_email == "real@lab.example.edu"

    def test_a_reconnect_does_not_rename_an_account_somebody_named(self, db: Session, template: _Template) -> None:
        user = UserFactory()
        connection = UserConnectionFactory(
            user=user,
            provider="whoop",
            provider_user_id="w-1",
            account_label="P01 left wrist",
            token_expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.commit()

        template._save_connection(
            db,
            user.id,
            _tokens(),
            {"user_id": "w-1", "username": None, "email": None},
            _state(user.id, connection_id=connection.id, account_label="something else"),
        )
        db.refresh(connection)

        assert connection.account_label == "P01 left wrist"
        assert connection.access_token == "new-access"
