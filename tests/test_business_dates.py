"""
Stored dates come from one clock, and it is not the reader's.

These are regression tests for a bug the suite found by being run in the evening:
``opened_on`` defaulted to ``timezone.localdate()``, which is the *acting user's*
date because ``apps/core/middleware.py`` activates their timezone per request. A
case created while one timezone was active and closed while a timezone behind it
was active could be stored as closing the day before it opened — which the check
constraint correctly refuses, with a 500 instead of a sentence.

The window was a few hours wide and moved with the seasons: the worst shape a bug
can have, because it works all day and fails after dinner.

Every test here uses two deliberately absurd timezones — Kiritimati at UTC+14 and
Niue at UTC−11, 25 hours apart — so the assertion holds at every hour of the day
rather than only during the hours that happened to expose it. See
apps/core/dates.py.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Role
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember
from apps.scheduling.models import AvailabilityRule, Weekday

pytestmark = pytest.mark.django_db

#: 25 hours apart, so "the same instant is two different dates" is true all day.
AHEAD = "Pacific/Kiritimati"  # UTC+14
BEHIND = "Pacific/Niue"  # UTC−11


@pytest.fixture
def org_is_ahead(settings):
    """Put the ministry in the furthest-forward timezone there is.

    With the org clock ahead of everything, any date taken from a viewer's own
    timezone is *behind* a stored one — which is the direction that breaks a
    constraint rather than the direction that quietly passes.
    """
    settings.ORG_TIME_ZONE = AHEAD
    return ZoneInfo(AHEAD)


@pytest.fixture
def viewing_from_behind():
    """Act as a user whose own timezone is a day behind the ministry's."""
    timezone.activate(ZoneInfo(BEHIND))
    yield
    timezone.deactivate()


class TestOrgToday:
    def test_it_ignores_whatever_timezone_is_active(self, org_is_ahead, viewing_from_behind):
        assert org_today() == timezone.now().astimezone(org_is_ahead).date()

    def test_it_is_not_the_viewers_date(self, org_is_ahead, viewing_from_behind):
        """The whole point, stated as an inequality.

        If these two were ever equal at every hour, this module would be pointless
        — so the fixtures are chosen to guarantee they differ.
        """
        assert org_today() != timezone.localdate()

    def test_a_nonsense_timezone_setting_falls_back_rather_than_raising(self, settings):
        """A mistyped ORG_TIME_ZONE must not be able to stop a case being opened."""
        settings.ORG_TIME_ZONE = "Mars/Olympus_Mons"

        assert org_today() == timezone.now().astimezone(ZoneInfo("UTC")).date()


class TestClosingACase:
    def test_a_case_can_be_closed_the_same_day_from_another_timezone(
        self, counselor, org_is_ahead, viewing_from_behind
    ):
        """The failure that started this file, in one line.

        Before the fix this raised IntegrityError: closed_on came from the reader's
        clock and landed a day before opened_on.
        """
        case = Case.objects.create(counselor=counselor, label="Concluded")

        case.close()

        case.refresh_from_db()
        assert case.closed_on == case.opened_on

    def test_the_view_can_close_it_too(self, client, sign_in, counselor, org_is_ahead):
        """Through the request, where the middleware activates the closer's own zone.

        The fixture above activates a timezone by hand; this asserts the same thing
        against the mechanism that actually does it in production.
        """
        counselor.timezone_name = BEHIND
        counselor.save(update_fields=["timezone_name"])
        case = Case.objects.create(counselor=counselor, label="Concluded")
        sign_in(counselor)

        from django.urls import reverse

        response = client.post(reverse("counseling:case_close", args=[case.public_id]))

        assert response.status_code == 302, "not a 500 from a violated constraint"
        case.refresh_from_db()
        assert case.closed_on == case.opened_on

    def test_an_explicit_date_is_still_honoured(self, counselor, org_is_ahead):
        """``close(on=...)`` is how a backdated closure is recorded, and it stays."""
        case = Case.objects.create(counselor=counselor, label="Concluded")
        yesterday = case.opened_on + timedelta(days=3)

        case.close(on=yesterday)

        assert case.closed_on == yesterday

    def test_a_genuinely_backwards_date_is_still_refused(self, counselor, org_is_ahead):
        """The constraint is not what was wrong, so it is asserted to still bite.

        A closure really before the opening is bad data. The fix was to stop
        *manufacturing* one, not to stop noticing.
        """
        case = Case.objects.create(counselor=counselor, label="Concluded")

        with pytest.raises(IntegrityError), transaction.atomic():
            case.close(on=case.opened_on - timedelta(days=1))


class TestEndingAMembership:
    def test_a_membership_can_end_the_day_it_began_from_another_timezone(
        self, counselor, make_user, org_is_ahead, viewing_from_behind
    ):
        """Somebody who leaves before the first session is the ordinary case here."""
        case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
        member = CaseMember.objects.create(case=case, counselee=make_user(Role.COUNSELEE))

        member.end()

        member.refresh_from_db()
        assert member.ended_on == member.joined_on


class TestOfficeHours:
    def test_a_window_starts_on_the_ministrys_date(
        self, counselor, org_is_ahead, viewing_from_behind
    ):
        """Same reasoning, and the same constraint shape — effective_to vs from."""
        rule = AvailabilityRule.objects.create(
            counselor=counselor,
            weekday=Weekday.TUESDAY,
            start_time="09:00",
            end_time="12:00",
        )

        assert rule.effective_from == org_today()
