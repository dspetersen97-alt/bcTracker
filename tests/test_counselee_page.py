"""
The "view counselee" page: one person's file on one screen.

What a counselor asked for, and the reason it needs its own tests rather than
inheriting the case page's: it *gathers*. Sessions, documents, and notes reached
individually are each scoped by their own queryset; put on one page keyed on a
person's id, the interesting question becomes what the gathering pulls in that the
viewer should not have.

So the claims here are about the edges of the page:

  * it exists only for someone holding a membership row for this counselee —
    another counselor gets 404, not 403, because 403 would confirm the account;
  * a counselee on cases with two different counselors does not bring one
    counselor's case history onto the other's screen;
  * a spouse's private upload is not filed under this person;
  * billing cannot open it at all, and is not shown the link.
"""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import Case, CaseMember
from apps.documents.models import Visibility
from apps.documents.services import store_document
from apps.scheduling.models import Booking, BookingStatus

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\nhomework\ntrailer\n"


@pytest.fixture
def ada(make_user):
    return make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")


@pytest.fixture
def ben(make_user):
    return make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")


@pytest.fixture
def marriage(counselor, ada, ben):
    case = Case.objects.create(
        counselor=counselor,
        label="Ashford — marriage",
        notes="Communication after a long illness.",
    )
    CaseMember.objects.create(case=case, counselee=ada)
    CaseMember.objects.create(case=case, counselee=ben)
    return case


@pytest.fixture
def make_booking(db):
    """An appointment, built directly rather than through ``services.book``.

    Nothing on this page depends on whether the time was bookable, and arranging
    office hours to line up with the notice rule would make these tests brittle for
    no gain.
    """
    now = timezone.now().replace(minute=0, second=0, microsecond=0)
    offsets = {"n": 0}

    def _make(case, counselee, *, days, **kwargs):
        offsets["n"] += 1
        return Booking.objects.create(
            counselor=case.counselor,
            case=case,
            counselee=counselee,
            slot=Booking.range_for(now + timedelta(days=days, hours=offsets["n"]), 60),
            status=BookingStatus.CONFIRMED,
            created_by=case.counselor,
            **kwargs,
        )

    return _make


def page_for(client, counselee):
    return client.get(reverse("counseling:counselee_detail", args=[counselee.pk]))


class TestWhatTheCounselorSees:
    def test_the_next_appointment_and_the_past_ones(
        self, client, sign_in, counselor, marriage, ada, make_booking
    ):
        last_week = make_booking(marriage, ada, days=-7)
        next_week = make_booking(marriage, ada, days=7)
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert reverse("scheduling:detail", args=[last_week.pk]) in body
        assert reverse("scheduling:detail", args=[next_week.pk]) in body

    def test_a_joint_session_counts_as_theirs_even_when_the_spouse_booked_it(
        self, client, sign_in, counselor, marriage, ada, ben, make_booking
    ):
        """Both of them were in the room.

        ``Booking.counselee`` on a joint appointment holds whoever arranged it, so a
        page filtering on that column alone would show a gap where a session was.
        """
        from apps.scheduling.models import Attendance

        joint = make_booking(marriage, ben, days=-3, attendance=Attendance.WHOLE_CASE)
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert reverse("scheduling:detail", args=[joint.pk]) in body

    def test_what_they_have_sent_in(self, client, sign_in, counselor, marriage, ada):
        document = store_document(
            case=marriage,
            owner=ada,
            upload=SimpleUploadedFile("week-one.pdf", PDF),
        )
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert reverse("documents:detail", args=[document.pk]) in body

    def test_a_document_filed_against_a_session_says_which(
        self, client, sign_in, counselor, marriage, ada, make_booking
    ):
        booking = make_booking(marriage, ada, days=-7)
        store_document(
            case=marriage,
            owner=ada,
            upload=SimpleUploadedFile("week-one.pdf", PDF),
            booking=booking,
        )
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert reverse("scheduling:detail", args=[booking.pk]) in body

    def test_their_own_session_notes(self, client, sign_in, counselor, marriage, ada, make_booking):
        session = make_booking(marriage, ada, days=-7)
        session.counselor_note = "Worked through the second worksheet."
        session.save(update_fields=["counselor_note", "updated_at"])
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert "Worked through the second worksheet." in body

    def test_and_the_presenting_concern(self, client, sign_in, counselor, marriage, ada):
        sign_in(counselor)

        assert "Communication after a long illness." in page_for(client, ada).content.decode()

    def test_looking_at_somebodys_file_is_recorded(self, client, sign_in, counselor, marriage, ada):
        """The question an access review asks is who read whose file.

        A page that gathers the record in one place is the clearest possible answer
        to that, so it gets its own verb rather than sharing ``case.viewed``.
        """
        sign_in(counselor)

        page_for(client, ada)

        assert AuditEvent.objects.filter(
            verb=AuditVerb.COUNSELEE_VIEWED, actor=counselor, target_id=str(ada.pk)
        ).exists()


class TestWhatTheGatheringDoesNotPullIn:
    def test_the_spouses_private_upload_is_not_filed_under_this_person(
        self, client, sign_in, counselor, marriage, ada, ben
    ):
        """The counselor may read it — on the case's documents page, where it is
        attributed to Ben. Under Ada's name it would be a different claim."""
        bens = store_document(
            case=marriage,
            owner=ben,
            upload=SimpleUploadedFile("bens-letter.pdf", PDF),
        )
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert reverse("documents:detail", args=[bens.pk]) not in body

    def test_but_something_shared_with_the_whole_case_is(
        self, client, sign_in, counselor, marriage, ada
    ):
        """A handout the counselor put in front of the case is in both files."""
        handout = store_document(
            case=marriage,
            owner=counselor,
            upload=SimpleUploadedFile("reading.pdf", PDF),
            visibility=Visibility.CASE_SHARED,
        )
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert reverse("documents:detail", args=[handout.pk]) in body

    def test_another_counselors_case_for_the_same_person_stays_theirs(
        self, client, sign_in, counselor, other_counselor, marriage, ada, make_booking
    ):
        """Ada is also seen individually by somebody else in the ministry.

        Her marriage counselor may not read that case's sessions, and the fact that
        both pages are about the same person is exactly why this is worth asserting
        here: the gathering is per membership the *viewer* holds.
        """
        elsewhere = Case.objects.create(counselor=other_counselor, label="Ashford — individual")
        CaseMember.objects.create(case=elsewhere, counselee=ada)
        theirs = make_booking(elsewhere, ada, days=-2)
        sign_in(counselor)

        body = page_for(client, ada).content.decode()

        assert "Ashford — individual" not in body
        assert reverse("scheduling:detail", args=[theirs.pk]) not in body


class TestWhoCanOpenIt:
    def test_a_counselor_with_no_case_for_this_person_gets_a_404(
        self, client, sign_in, other_counselor, marriage, ada
    ):
        """404 rather than 403: a refusal would confirm the account exists."""
        sign_in(other_counselor)

        assert page_for(client, ada).status_code == 404

    def test_an_administrator_can_open_anybodys(self, client, sign_in, admin_user, marriage, ada):
        sign_in(admin_user)

        assert page_for(client, ada).status_code == 200

    def test_billing_is_refused(self, client, sign_in, financial_admin, marriage, ada):
        sign_in(financial_admin)

        assert page_for(client, ada).status_code == 403

    def test_a_counselee_cannot_open_their_own_this_way(self, client, sign_in, marriage, ada):
        """Their own version of this page is their dashboard. A route keyed on a
        person's id is an invitation to try somebody else's."""
        sign_in(ada)

        assert page_for(client, ada).status_code == 403

    def test_a_membership_that_ended_still_opens_the_file(
        self, client, sign_in, counselor, marriage, ada
    ):
        """Counseling that has finished is still the counselor's record of it, and
        the case page keeps ended memberships for the same reason."""
        CaseMember.objects.filter(case=marriage, counselee=ada).update(ended_on=marriage.opened_on)
        sign_in(counselor)

        assert page_for(client, ada).status_code == 200


class TestItIsReachableByClicking:
    def test_from_the_case_page(self, client, sign_in, counselor, marriage, ada):
        sign_in(counselor)

        body = client.get(reverse("counseling:case_detail", args=[marriage.pk])).content.decode()

        assert reverse("counseling:counselee_detail", args=[ada.pk]) in body

    def test_and_from_the_caseload(self, client, sign_in, counselor, marriage, ada):
        sign_in(counselor)

        body = client.get(reverse("counseling:counselor_dashboard")).content.decode()

        assert reverse("counseling:counselee_detail", args=[ada.pk]) in body

    def test_billing_is_not_offered_the_link(self, client, sign_in, financial_admin, marriage, ada):
        """Billing reads the case page for the roster. A link to a page that would
        refuse them is worse than no link, and the page it leads to is the whole
        counseling record."""
        sign_in(financial_admin)

        body = client.get(reverse("counseling:case_detail", args=[marriage.pk])).content.decode()

        assert "Ashford" in body, "billing should still see the case and its roster"
        assert reverse("counseling:counselee_detail", args=[ada.pk]) not in body
