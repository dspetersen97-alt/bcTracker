"""
What can go wrong with Google, sorted by what the caller should do about it.

The taxonomy is the useful part. Every call site has to answer the same question
— retry, give up quietly, or make the counselor act — and getting that wrong in
either direction is bad: treating an expired grant as transient means retrying
every 15 minutes for ever while the counselor's calendar stays empty and nobody
is told, and treating a 503 as permanent means disconnecting a working
integration because Google had a bad minute.
"""


class GoogleError(Exception):
    """Base. Never raised directly."""


class GoogleNotConfigured(GoogleError):
    """No client id, or the integration is switched off.

    A configuration mistake rather than a runtime failure, so it is not retried
    and not recorded against the counselor's connection — there is nothing they
    could do about it.
    """


class TransientGoogleError(GoogleError):
    """A timeout, a 5xx, or a rate limit. Try again on the next tick.

    Deliberately not recorded as the connection's ``last_error`` on its own: a
    counselor should not be shown "sync failed" because one request out of a
    hundred timed out and the next one worked.
    """


class GoogleRefused(GoogleError):
    """Google understood the request and said no. A bug on our side, usually.

    A 400 or a 403 that is not an authorization problem — a malformed event body,
    a calendar id that does not exist. Retrying identical input will fail
    identically, so the sync gives up on that booking and logs loudly.
    """


class AuthorizationLost(GoogleError):
    """The grant is gone. Only the counselor can fix it, by reconnecting.

    Raised for ``invalid_grant`` on refresh — which covers a revoked grant, a
    password change, and a token unused for six months — and for a 401 that
    survives a refresh. The credential is marked revoked so the counselor is
    asked to reconnect instead of the ministry finding out weeks later that
    nothing has reached their calendar.
    """


class EventGone(GoogleError):
    """The event we recorded an id for is no longer in the calendar.

    Not an error in itself: a counselor deleting the event in Google is the
    ordinary way this happens. The booking is authoritative, so the sync forgets
    the stale id and inserts a fresh event.
    """
