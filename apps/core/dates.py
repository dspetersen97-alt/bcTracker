"""
What day it is, for the ministry rather than for whoever is looking.

Timestamps are stored in UTC and rendered in the viewer's own timezone — that is
what ``apps/core/middleware.py`` activates a timezone for, and scheduling depends
on it. But a *business date* is not a rendering: the day a case opened, the day a
membership ended, the day an invoice is dated are facts about the ministry, and
they must not depend on who wrote the row.

``timezone.localdate()`` does depend on that, because the active timezone is the
acting user's. At 21:00 in New York it is already tomorrow in UTC, so a case
created by a counselor and closed the same evening by an administrator in another
zone could be stored as closing the day *before* it opened — which the check
constraints in ``apps/counseling/models.py`` correctly refuse, with a 500 rather
than a sentence. The window is a few hours wide and moves with the seasons, which
is the worst shape a bug can have: it works all day and fails in the evening.

So business dates come from here, in ``settings.ORG_TIME_ZONE``, and
``timezone.localdate()`` stays where it belongs — deciding what to *show*
somebody, such as which calendar exceptions are still in the future.
"""

from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone


def org_zone() -> ZoneInfo:
    """The ministry's timezone, falling back to UTC rather than raising.

    A mistyped ``ORG_TIME_ZONE`` should not be able to stop a case being opened.
    ``config/settings/checks.py`` is where a bad value is meant to be caught; this
    is the belt to that braces.
    """
    try:
        return ZoneInfo(settings.ORG_TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def org_today() -> date:
    """Today's date in the ministry's timezone.

    Used as the default for every stored business date. Named as a module-level
    function because a field default has to be importable by a migration.
    """
    return timezone.now().astimezone(org_zone()).date()
