"""
The calendar a counselee picks a day out of.

``tests/test_slots.py`` covers the grid arithmetic with no database in sight, and
``tests/test_scheduling_access.py`` covers the three steps of actually booking. What
is left for here is the part that only exists once there is a request: which month
is drawn, which day's times are listed, and what happens when the query string says
something that is no longer true.

The recurring theme is that ``?day`` and ``?month`` come from links on the page and
are therefore also things a person can type, bookmark, and come back to a fortnight
later. None of them may produce an error page — the commonest way to arrive at a day
with nothing on it is a bookmark of a time somebody else has since taken.
"""

from datetime import date, timedelta

import pytest
from django.urls import reverse

from apps.counseling.models import Case, CaseMember, CounselorProfile
from apps.scheduling.models import AvailabilityOverride, AvailabilityRule, Weekday

pytestmark = pytest.mark.django_db


@pytest.fixture
def case(counselor, counselee):
    CounselorProfile.objects.create(user=counselor)
    case = Case.objects.create(counselor=counselor, label="Marsh — individual")
    CaseMember.objects.create(case=case, counselee=counselee)
    return case


def open_all_week(counselor, *, start="09:00", end="17:00"):
    for weekday in Weekday.values:
        AvailabilityRule.objects.create(
            counselor=counselor, weekday=weekday, start_time=start, end_time=end
        )


@pytest.fixture
def booking_page(case, counselee, client, sign_in):
    """Signed in as the counselee, with the URL of their booking page."""
    sign_in(counselee)
    return reverse("scheduling:book", kwargs={"case_public_id": case.public_id})


def cells(response):
    """Every calendar cell of a rendered page, keyed by date."""
    return {cell.date: cell for week in response.context["calendar"] for cell in week}


def months_ahead(count):
    """The first of the month ``count`` months from now. Never a day-of-month bug."""
    this_month = date.today().replace(day=1)
    total = this_month.year * 12 + this_month.month - 1 + count
    return date(total // 12, total % 12 + 1, 1)


def shorten_horizon(counselor, days):
    """Bring the counselor's booking horizon in, so a nearby month is empty.

    Nudging the horizon rather than picking a month far enough out to be blank by
    arithmetic: which month that is depends on the day the suite happens to run,
    which is how a test comes to pass all year and fail in November.
    """
    profile = CounselorProfile.objects.get(user=counselor)
    profile.booking_horizon_days = days
    profile.save(update_fields=["booking_horizon_days"])


# --- what the grid says ---------------------------------------------------


class TestTheGrid:
    def test_the_month_is_drawn(self, case, booking_page, client):
        open_all_week(case.counselor)

        response = client.get(booking_page)

        assert response.status_code == 200
        assert "calendar" in response.content.decode()
        assert response.context["month"].day == 1
        assert len(response.context["weekdays"]) == 7

    def test_a_day_with_times_is_a_link_that_says_how_many(self, case, booking_page, client):
        """The highlight catches the eye across a month; the count is what makes the
        cell readable to somebody who cannot see the highlight at all."""
        open_all_week(case.counselor)

        response = client.get(booking_page)
        page = response.content.decode()
        day = response.context["selected_day"]

        assert f"?day={day.isoformat()}" in page
        assert "8 times" in page

    def test_a_day_with_nothing_is_not_a_link(self, case, booking_page, client):
        """Only Wednesdays are open, so every other cell must be inert."""
        AvailabilityRule.objects.create(
            counselor=case.counselor,
            weekday=Weekday.WEDNESDAY,
            start_time="09:00",
            end_time="12:00",
        )

        response = client.get(booking_page)
        open_days = [day for day, cell in cells(response).items() if cell.is_available]

        assert open_days, "the fixture should leave some Wednesdays open"
        assert {day.weekday() for day in open_days} == {Weekday.WEDNESDAY}

    def test_a_closure_takes_its_day_out_of_the_grid(self, case, booking_page, client):
        """The point of the highlight: the shut day is visibly not one to click."""
        open_all_week(case.counselor)
        shut = date.today() + timedelta(days=5)
        AvailabilityOverride.objects.create(counselor=case.counselor, date=shut, is_available=False)

        response = client.get(booking_page)

        assert not cells(response)[shut].is_available

    def test_the_grid_stops_at_the_counselors_horizon(self, case, booking_page, client):
        """Whatever the calendar draws, the horizon is what may be booked."""
        open_all_week(case.counselor)
        profile = CounselorProfile.objects.get(user=case.counselor)
        profile.booking_horizon_days = 10
        profile.save(update_fields=["booking_horizon_days"])

        response = client.get(booking_page)
        available = [day for day, cell in cells(response).items() if cell.is_available]

        assert max(available) <= date.today() + timedelta(days=10)


class TestWhichDayIsChosen:
    def test_the_page_arrives_with_the_soonest_day_already_showing(
        self, case, booking_page, client
    ):
        """Landing on an instruction to click something would waste the first click."""
        open_all_week(case.counselor)

        response = client.get(booking_page)

        assert response.context["selected_day"] is not None
        assert response.context["days"], "the chosen day's times should be listed"
        assert cells(response)[response.context["selected_day"]].is_selected

    def test_asking_for_a_day_lists_that_day(self, case, booking_page, client):
        open_all_week(case.counselor)
        first = client.get(booking_page).context["selected_day"]
        later = next(
            day
            for day, cell in sorted(cells(client.get(booking_page)).items())
            if cell.is_available and day > first
        )

        response = client.get(booking_page, {"day": later.isoformat()})

        assert response.context["selected_day"] == later
        assert [day for day, _times in response.context["days"]] == [later]

    def test_asking_for_a_day_with_nothing_on_it_falls_back(self, case, booking_page, client):
        """A bookmark of a time since taken must not be an error page."""
        AvailabilityRule.objects.create(
            counselor=case.counselor,
            weekday=Weekday.WEDNESDAY,
            start_time="09:00",
            end_time="12:00",
        )
        response = client.get(booking_page)
        wednesday = response.context["selected_day"]

        fallen_back = client.get(booking_page, {"day": (wednesday + timedelta(days=1)).isoformat()})

        assert fallen_back.status_code == 200
        assert fallen_back.context["selected_day"] == wednesday

    def test_the_month_follows_the_chosen_day(self, case, booking_page, client):
        open_all_week(case.counselor)
        next_month = (date.today().replace(day=1) + timedelta(days=45)).replace(day=15)

        response = client.get(booking_page, {"day": next_month.isoformat()})

        assert response.context["month"].month == next_month.month

    def test_nonsense_in_the_query_string_is_ignored(self, case, booking_page, client):
        open_all_week(case.counselor)

        for query in ({"day": "yesterday"}, {"month": "2026-13"}, {"month": "soon"}, {"day": ""}):
            response = client.get(booking_page, query)
            assert response.status_code == 200, query
            assert response.context["selected_day"] is not None, query

    def test_a_day_before_today_is_not_offered(self, case, booking_page, client):
        open_all_week(case.counselor)

        response = client.get(booking_page, {"day": (date.today() - timedelta(days=3)).isoformat()})

        assert response.context["selected_day"] >= date.today()


class TestPagingThroughMonths:
    def test_this_month_has_nothing_earlier(self, case, booking_page, client):
        """Paging back into last year would only ever show blank cells."""
        open_all_week(case.counselor)

        response = client.get(booking_page)

        assert response.context["previous_month"] is None

    def test_a_later_month_can_be_paged_back_from(self, case, booking_page, client):
        open_all_week(case.counselor)
        later = (date.today().replace(day=1) + timedelta(days=45)).replace(day=1)

        response = client.get(booking_page, {"month": later.strftime("%Y-%m")})

        assert response.context["month"] == later
        assert response.context["previous_month"] is not None

    def test_paging_forward_stops_at_the_end_of_the_span(self, case, booking_page, client):
        """Four months out every cell is blank, so there is nothing to page to."""
        open_all_week(case.counselor)
        far = (date.today().replace(day=1) + timedelta(days=150)).replace(day=1)

        response = client.get(booking_page, {"month": far.strftime("%Y-%m")})

        assert response.context["next_month"] is None

    def test_an_empty_month_says_which_month_is_empty(self, case, booking_page, client):
        """Not the same wording as "nothing at all": the next step is different, and
        one message for both would send somebody paging through blank months."""
        AvailabilityRule.objects.create(
            counselor=case.counselor,
            weekday=Weekday.WEDNESDAY,
            start_time="09:00",
            end_time="10:00",
        )
        shorten_horizon(case.counselor, 21)
        far = months_ahead(2)

        page = client.get(booking_page, {"month": far.strftime("%Y-%m")}).content.decode()

        assert "Nothing free in" in page
        assert "No times available at the moment" not in page

    def test_a_counselor_with_no_hours_at_all_gets_the_other_message(
        self, case, booking_page, client
    ):
        page = client.get(booking_page).content.decode()

        assert "No times available at the moment" in page
        assert "Nothing free in" not in page

    def test_asking_for_an_empty_month_shows_that_month_rather_than_jumping_back(
        self, case, booking_page, client
    ):
        """Otherwise paging forward past the last free day silently undoes itself."""
        AvailabilityRule.objects.create(
            counselor=case.counselor,
            weekday=Weekday.WEDNESDAY,
            start_time="09:00",
            end_time="10:00",
        )
        shorten_horizon(case.counselor, 21)
        far = months_ahead(2)

        response = client.get(booking_page, {"month": far.strftime("%Y-%m")})

        assert response.context["month"] == far
        assert response.context["selected_day"] is None


class TestPickingATimeFromTheChosenDay:
    def test_the_times_link_on_to_the_confirmation_step(self, case, booking_page, client):
        open_all_week(case.counselor)

        response = client.get(booking_page)
        slot = response.context["days"][0][1][0]

        picked = client.get(booking_page, {"slot": slot.start.isoformat()})

        assert picked.context["chosen"] == slot.start
        assert picked.context["form"] is not None

    def test_going_back_from_the_confirmation_returns_to_that_day(self, case, booking_page, client):
        """ "Pick a different time" should land on the day they were looking at."""
        open_all_week(case.counselor)
        slot = client.get(booking_page).context["days"][0][1][0]

        page = client.get(booking_page, {"slot": slot.start.isoformat()}).content.decode()

        assert f"?day={slot.start.date().isoformat()}" in page

    def test_a_counselor_looking_at_the_page_sees_the_calendar_too(self, case, client, sign_in):
        """They are checking what their counselee would see, so it has to be the same
        page — and it must not offer to book them an appointment with themselves."""
        open_all_week(case.counselor)
        sign_in(case.counselor)

        response = client.get(reverse("scheduling:book", kwargs={"case_public_id": case.public_id}))

        assert response.context["may_book"] is False
        assert response.context["calendar"]
