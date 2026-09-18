"""The provider account a unit of work is currently acting for.

Almost every provider call resolves its credentials from ``(user_id, provider)``
- ``_get_valid_token``, the webhook handlers, the Garmin backfill tasks. That
was unambiguous while a user could hold one account per provider. It is not any
more: a participant wearing two Whoops has two rows, two access tokens and two
sets of samples, and a lookup by ``(user_id, provider)`` alone would pick one of
them arbitrarily and file the second unit's data under the first.

Rewriting every provider method to carry a connection id would mean touching
every signature in ``app/services/providers`` and every call site behind it. The
context variable here does the same job at the two points that matter: the
caller that already knows which account it is working for (the sync task loop,
the webhook handler that just resolved a connection from a provider user id)
declares it once, and the resolution inside ``UserConnectionRepository`` honours
it. Everything in between stays as it is.

Outside such a scope the repository falls back to the oldest active connection
and logs it, which is the pre-multi-account behaviour.

A Celery task runs in one thread per execution and a ``ContextVar`` is bound to
that thread, so two accounts syncing concurrently cannot read each other's
scope. The value is a connection *id* rather than an ORM object on purpose: an
object would outlive its session.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_active_connection_id: ContextVar[UUID | None] = ContextVar("active_connection_id", default=None)


def get_active_connection_id() -> UUID | None:
    """The connection the current unit of work is acting for, if it declared one."""
    return _active_connection_id.get()


@contextmanager
def active_connection(connection_id: UUID | None) -> Iterator[None]:
    """Bind provider credential lookups to one account for the duration of the block.

    ``None`` clears any inherited scope rather than leaving the enclosing one in
    place, so a nested block cannot silently borrow an outer account.
    """
    token = _active_connection_id.set(connection_id)
    try:
        yield
    finally:
        _active_connection_id.reset(token)


def bind_active_connection(connection_id: UUID | None) -> None:
    """Set the scope imperatively, for loops that cannot wrap a block.

    The sync task walks a user's connections one at a time and each iteration
    rebinds before it does anything, so an earlier account's scope can never be
    read by a later one. What it cannot do is leave the scope set when the task
    ends - a Celery worker reuses its threads, and the next task on that thread
    would inherit an account that has nothing to do with it - so every caller of
    this function must clear the scope in a ``finally`` that covers the whole
    loop. Prefer :func:`active_connection` wherever a ``with`` block fits.
    """
    _active_connection_id.set(connection_id)


def clear_active_connection() -> None:
    """Drop any imperatively bound scope. Safe to call when none is set."""
    _active_connection_id.set(None)
