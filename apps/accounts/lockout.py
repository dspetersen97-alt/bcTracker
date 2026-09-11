"""
What a locked-out sign-in attempt sees, and what gets recorded about it.

django-axes counts the failures and decides when to refuse; the two things it
should not decide for us are both here.

**What the page says.** The default lockout page names the address and the
failure limit. This one does not: the login form is reachable by anyone on the
internet, and a page that says "this account is locked" where an unknown address
gets an ordinary "wrong password" would turn five requests into a way to find out
who has an account at a counseling ministry. The response is the same either way,
and it explains the cooloff rather than leaving the reader to guess at it.

**What is recorded.** One audit row when the lockout begins, not one per refused
request during the cooloff — a client that keeps trying would otherwise bury the
trail in identical rows. That is why the row is written from the signal, which
fires once, rather than from the response below, which is rendered every time.
"""

import math
from datetime import timedelta

from axes.signals import user_locked_out
from django.conf import settings
from django.dispatch import receiver
from django.shortcuts import render
from django.utils import timezone

from apps.audit.models import AuditVerb
from apps.audit.services import record


def cooloff_minutes() -> int:
    """How long the lockout lasts, rounded up, for the page to state.

    Rounded up rather than down so that somebody who waits exactly as long as
    they were told is not refused again by a few seconds.
    """
    return math.ceil(_cooloff().total_seconds() / 60)


def locked_out(request, original_response=None, credentials=None):
    """The response to an attempt made while locked out.

    Three positional parameters because that is the signature django-axes tries
    first; it falls back to a two-argument call on ``TypeError``, which would
    swallow a genuine ``TypeError`` raised in here — so nothing in this function
    may raise one.

    429 rather than a redirect back to the form: a redirect would let a client
    loop without ever being told why, and the status code is the one part of this
    response a script reads.
    """
    return render(
        request,
        "accounts/locked_out.html",
        {"cooloff_minutes": cooloff_minutes()},
        status=429,
    )


@receiver(user_locked_out)
def record_lockout(sender, request=None, username="", ip_address="", **kwargs):
    """Write the audit row for a lockout starting.

    ``username`` is whatever was submitted, which may well be an address with no
    account — that is the interesting case, and recording it is how a run of
    attempts against invented addresses becomes visible. It is not an assertion
    that the account exists.

    django-axes sends this on every refused request, not only the first, so the
    row is written once per cooloff window. Without that, how many rows land in a
    table that is append-only and cannot be pruned would be up to whoever is
    hammering the form — and the run of LOGIN_FAILED rows already records each
    attempt that got as far as being counted.
    """
    email = str(username or "")[:254]
    ip = str(ip_address or "")
    if _already_recorded(email=email, ip=ip):
        return

    record(
        AuditVerb.LOGIN_LOCKED_OUT,
        request=request,
        method="password",
        email=email,
        # Recorded even though ``record()`` derives an IP from the request, so
        # that the row still says where from if the lockout was decided by a
        # handler that saw a different address than the request headers imply.
        ip_address=ip,
        failure_limit=settings.AXES_FAILURE_LIMIT,
    )


def _already_recorded(*, email: str, ip: str) -> bool:
    """Whether this lockout has been recorded during the current cooloff.

    Matched on the pair the lockout is keyed by, so a second address from the same
    place — or the same address from somewhere else — still gets its own row.
    """
    from apps.audit.models import AuditEvent

    return AuditEvent.objects.filter(
        verb=AuditVerb.LOGIN_LOCKED_OUT,
        created_at__gte=timezone.now() - _cooloff(),
        metadata__email=email,
        metadata__ip_address=ip,
    ).exists()


def _cooloff() -> timedelta:
    cooloff = getattr(settings, "AXES_COOLOFF_TIME", None)
    if cooloff is None:
        # No cooloff configured means the lockout is permanent until cleared, so
        # one row for it is right and the window only has to be wide enough not
        # to write a second.
        return timedelta(days=1)
    return cooloff if isinstance(cooloff, timedelta) else timedelta(hours=cooloff)
