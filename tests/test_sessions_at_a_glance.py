"""
"When did I last see them, and when do I see them next."

Two pages answer that question and they share the machinery in
``apps/scheduling/services.py``: a counselor's caseload gets a column each way,
and a counselee's home page gets the next one, large, at the top.

What is worth testing here is not that a date appears — it is the three judgements
underneath the dates, each of which is a thing that would be quietly wrong:

  * A **cancelled** appointment in the past is not a session that happened, so it
    is never "last session". A no-show is: it says when contact was last arranged,
    and a counselor scanning for people they have lost touch with needs to see it
    rather than have it silently skipped.
  * A **requested-but-unconfirmed** appointment is "next session". Hiding it until
    the counselor agrees would tell a counselee that nothing is booked seconds
    after they booked something, and they would book it again.
  * Both are read through ``for_actor``, so a table that decorates thirty rows
    cannot become the place where scoping is forgotten.

The query count is asserted too. A dashboard that grows two queries per case is
fine on four rows and slow on the only machine that matters.
"""

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.counseling.models import Case, CaseMember, CounselorProfile
from apps.scheduling import services
from apps.scheduling.models import Attendance, Booking, BookingStatus

pytestmark = pytest.mark.django_db

CASELOAD = reverse("counseling:counselor_dashboard")
HOME = reverse("core:home")


@pytest.fixture
def practice(counselor, counselee):
    CounselorProfile.objects.create(user=counselor)
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    CaseMember.objects.create(case=case, counselee=counselee)
    return case


def appointment(case, counselee, *, days, status=BookingStatus.CONFIRMED, **kwargs):
    """One appointment ``days`` from now, positive for future and negative for past.

    Written straight onto the model rather than through ``services.book``, because
    these tests need appointments in the past and booking one is refused — which is
    correct, and is a different rule from the one under test.

    ``cancelled_at`` is filled in for a cancelled one because a check constraint
    insists on it, and rightly: a cancellation with no time on it cannot be judged
    late, and lateness is what decides whether it is billable.
    """
    if status == BookingStatus.CANCELLED:
        kwargs.setdefault("cancelled_at", timezone.now())
    start = timezone.now() + timedelta(days=days)
    return Booking.objects.create(
        case=case,
        counselor=case.counselor,
        counselee=counselee,
        slot=Booking.range_for(start, 60),
        status=status,
        created_by=case.counselor,
        **kwargs,
    )


def caseload_row(client):
    return client.get(CASELOAD).content.decode()


# --- the two columns on a caseload ----------------------------------------


class TestTheCaseloadColumns:
    def test_both_columns_are_there(self, practice, client, sign_in):
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert "Last session" in page
        assert "Next session" in page

    def test_the_last_session_links_to_that_appointment(self, practice, counselee, client, sign_in):
        was = appointment(practice, counselee, days=-7, status=BookingStatus.COMPLETED)
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert reverse("scheduling:detail", kwargs={"public_id": was.public_id}) in page

    def test_the_next_session_links_to_that_appointment(self, practice, counselee, client, sign_in):
        soon = appointment(practice, counselee, days=3)
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert reverse("scheduling:detail", kwargs={"public_id": soon.public_id}) in page

    def test_it_is_the_nearest_one_each_way(self, practice, counselee, client, sign_in):
        """Not simply "an appointment on this case" — the two that bracket today."""
        long_ago = appointment(practice, counselee, days=-30, status=BookingStatus.COMPLETED)
        recently = appointment(practice, counselee, days=-3, status=BookingStatus.COMPLETED)
        soon = appointment(practice, counselee, days=2)
        later = appointment(practice, counselee, days=20)
        sign_in(practice.counselor)

        page = caseload_row(client)

        def link(booking):
            return reverse("scheduling:detail", kwargs={"public_id": booking.public_id})

        assert link(recently) in page
        assert link(soon) in page
        assert link(long_ago) not in page
        assert link(later) not in page

    def test_a_case_with_nothing_says_so_in_words(self, practice, client, sign_in):
        """Not a dash: "—" is read aloud as "em dash" and says nothing."""
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert "Not met yet" in page
        assert "Nothing booked" in page

    def test_another_counselors_case_is_not_on_this_page(
        self, practice, other_counselor, counselee, client, sign_in
    ):
        theirs = Case.objects.create(counselor=other_counselor, label="Not mine")
        CaseMember.objects.create(case=theirs, counselee=counselee)
        not_mine = appointment(theirs, counselee, days=2)
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert "Not mine" not in page
        assert reverse("scheduling:detail", kwargs={"public_id": not_mine.public_id}) not in page

    def test_the_page_costs_the_same_whatever_the_caseload(
        self, practice, counselee, client, sign_in
    ):
        """The reason ``attach_sessions`` takes a list rather than being a property.

        Written as one caseload against seven rather than against a fixed number,
        because the interesting property is the *slope* and a fixed number is a test
        that fails whenever anything else on the page gains a query. A per-case
        version of this would add twelve statements between the two measurements.
        """
        sign_in(practice.counselor)

        with CaptureQueriesContext(connection) as one_case:
            assert client.get(CASELOAD).status_code == 200

        for index in range(6):
            case = Case.objects.create(counselor=practice.counselor, label=f"Case {index}")
            CaseMember.objects.create(case=case, counselee=counselee)
            appointment(case, counselee, days=-index - 1, status=BookingStatus.COMPLETED)
            appointment(case, counselee, days=index + 1)

        with CaptureQueriesContext(connection) as seven_cases:
            assert client.get(CASELOAD).status_code == 200

        assert len(seven_cases) == len(one_case)


class TestWhatCountsAsHavingHappened:
    def test_a_cancelled_appointment_is_not_a_last_session(self, practice, counselee):
        appointment(practice, counselee, days=-4, status=BookingStatus.CANCELLED)

        assert services.last_appointment(actor=practice.counselor) is None

    def test_a_no_show_is(self, practice, counselee):
        """It is when contact was last arranged, which is the thing being asked."""
        missed = appointment(practice, counselee, days=-4, status=BookingStatus.NO_SHOW)

        assert services.last_appointment(actor=practice.counselor) == missed

    def test_a_past_appointment_nobody_wrote_up_is(self, practice, counselee):
        """Counselors do not always record an outcome, and the session still happened."""
        held = appointment(practice, counselee, days=-4, status=BookingStatus.CONFIRMED)

        assert services.last_appointment(actor=practice.counselor) == held

    def test_a_no_show_says_so_on_the_caseload(self, practice, counselee, client, sign_in):
        """A date on its own would read as a session that took place."""
        appointment(practice, counselee, days=-4, status=BookingStatus.NO_SHOW)
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert "did not attend" in page.lower()

    def test_a_confirmed_appointment_is_not_labelled(self, practice, counselee, client, sign_in):
        """A column of "confirmed" is noise, and noise is what hides "did not attend"."""
        appointment(practice, counselee, days=3, status=BookingStatus.CONFIRMED)
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert "status-confirmed" not in page

    def test_a_requested_appointment_is_labelled(self, practice, counselee, client, sign_in):
        appointment(practice, counselee, days=3, status=BookingStatus.REQUESTED)
        sign_in(practice.counselor)

        page = caseload_row(client)

        assert "status-requested" in page

    def test_a_cancelled_appointment_is_not_the_next_session_either(self, practice, counselee):
        appointment(practice, counselee, days=3, status=BookingStatus.CANCELLED)

        assert services.next_appointment(actor=practice.counselor) is None


# --- a counselee's home page ----------------------------------------------


class TestTheNextSessionBlock:
    def test_it_is_the_first_thing_after_the_welcome(self, practice, counselee, client, sign_in):
        soon = appointment(practice, counselee, days=2)
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert "Your next session" in page
        assert reverse("scheduling:detail", kwargs={"public_id": soon.public_id}) in page
        # Above the tiles, which is the whole of the request: it is the biggest thing
        # on the page, not one card among six.
        assert page.index("Your next session") < page.index("nav-cards")

    def test_it_says_who_it_is_with(self, practice, counselee, client, sign_in):
        appointment(practice, counselee, days=2)
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert practice.counselor.full_name in page

    def test_it_names_no_case(self, practice, counselee, client, sign_in):
        """The counselee's own page, but a case label is still a label about them."""
        appointment(practice, counselee, days=2)
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert "Ashford" not in page

    def test_a_virtual_session_can_be_joined_from_here(self, practice, counselee, client, sign_in):
        appointment(practice, counselee, days=2, meeting_url="https://meet.example.org/ada")
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert "https://meet.example.org/ada" in page
        assert 'rel="noopener noreferrer"' in page

    def test_an_unconfirmed_request_is_shown_and_marked(self, practice, counselee, client, sign_in):
        appointment(practice, counselee, days=2, status=BookingStatus.REQUESTED)
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert "Your next session" in page
        assert "Waiting for them to confirm" in page

    def test_with_nothing_booked_it_offers_the_way_to_book(
        self, practice, counselee, client, sign_in
    ):
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert "Nothing booked yet" in page
        assert reverse("counseling:my_cases") in page

    def test_a_past_appointment_is_not_a_next_session(self, practice, counselee, client, sign_in):
        appointment(practice, counselee, days=-2, status=BookingStatus.COMPLETED)
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert "Nothing booked yet" in page

    def test_a_counselor_gets_no_such_block(self, practice, counselee, client, sign_in):
        """Their next appointment is one row of the caseload they are about to open."""
        appointment(practice, counselee, days=2)
        sign_in(practice.counselor)

        page = client.get(HOME).content.decode()

        assert "Your next session" not in page
        assert "Nothing booked yet" not in page

    def test_another_counselees_individual_session_is_not_shown(
        self, practice, counselee, make_user, client, sign_in
    ):
        """A family case is not a shared calendar. Same rule as documents.

        The sibling's appointment is sooner, so a version of this that forgot to scope
        would show it — which is the point of asserting on a sooner one.
        """
        ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
        CaseMember.objects.create(case=practice, counselee=ben)
        theirs = appointment(practice, ben, days=1, attendance=Attendance.INDIVIDUAL)
        mine = appointment(practice, counselee, days=5)
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert reverse("scheduling:detail", kwargs={"public_id": mine.public_id}) in page
        assert reverse("scheduling:detail", kwargs={"public_id": theirs.public_id}) not in page

    def test_a_joint_session_is_shown_to_everyone_expected_at_it(
        self, practice, counselee, make_user, client, sign_in
    ):
        ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
        CaseMember.objects.create(case=practice, counselee=ben)
        together = appointment(practice, ben, days=1, attendance=Attendance.WHOLE_CASE)
        sign_in(counselee)

        page = client.get(HOME).content.decode()

        assert reverse("scheduling:detail", kwargs={"public_id": together.public_id}) in page
