"""
Uploading a document for a particular appointment.

The counselee's side of the two-way exchange, arriving from the place they were
already looking: the diary. Homework is handed in *for* a session — before it or
after it — and a counselor asked for it to turn up on that session rather than at
the bottom of one case-wide pile.

``Document.booking`` is a label and never a route, so what these tests are mostly
about is that the label cannot be used to widen anything:

  * the booking is resolved through ``Booking.objects.for_actor`` narrowed to the
    case the upload is going onto, so an id from another case — or from a spouse's
    individual appointment — files nothing;
  * a bad id loses the link, not the upload;
  * the list on the session page is ``Document.objects.for_actor``, so a spouse's
    private upload is absent from a joint session and billing sees no list at all.
"""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.counseling.models import Case, CaseMember
from apps.documents.models import Document, Visibility
from apps.scheduling.models import Attendance, Booking, BookingStatus

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\nweek one\ntrailer\n"


def upload_payload(name="homework.pdf", data=PDF, **extra):
    return {
        "file": SimpleUploadedFile(name, data),
        "title": "",
        "description": "",
        "kind": "homework",
        **extra,
    }


def upload_url(case):
    return reverse("documents:upload", kwargs={"case_pk": case.pk})


@pytest.fixture
def make_booking(db):
    """An appointment, built directly.

    Not through ``services.book``: that needs office hours arranged to line up
    with a slot the notice rule also allows, which is brittle and beside the point
    — nothing here is about whether the time was bookable.
    """
    now = timezone.now().replace(minute=0, second=0, microsecond=0)
    offsets = {"n": 0}

    def _make(case, counselee, *, days=3, **kwargs):
        offsets["n"] += 1
        return Booking.objects.create(
            counselor=case.counselor,
            case=case,
            counselee=counselee,
            slot=Booking.range_for(now + timedelta(days=days, hours=offsets["n"]), 60),
            status=BookingStatus.CONFIRMED,
            created_by=counselee,
            **kwargs,
        )

    return _make


@pytest.fixture
def couple_case(counselor, make_user):
    """Ada and Ben on one case: the shape the privacy promise is about."""
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    ada = make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")
    ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=ada)
    CaseMember.objects.create(case=case, counselee=ben)
    return case, ada, ben


class TestFilingAnUploadAgainstASession:
    def test_a_counselee_can_send_a_document_for_an_appointment(
        self, client, sign_in, couple_case, make_booking
    ):
        case, ada, _ben = couple_case
        booking = make_booking(case, ada)
        sign_in(ada)

        response = client.post(upload_url(case), upload_payload(booking=booking.pk))

        document = Document.objects.get()
        assert document.booking_id == booking.pk
        # Back to the appointment, where it now appears — the confirmation somebody
        # uploading homework actually wants.
        assert response.status_code == 302
        assert response.headers["Location"] == reverse("scheduling:detail", args=[booking.pk])

    def test_an_upload_with_no_session_still_belongs_to_the_case(
        self, client, sign_in, couple_case
    ):
        """The ordinary path, unchanged. Most documents are not about a Tuesday."""
        case, ada, _ben = couple_case
        sign_in(ada)

        response = client.post(upload_url(case), upload_payload())

        assert Document.objects.get().booking_id is None
        assert response.headers["Location"] == reverse(
            "documents:case_documents", kwargs={"case_pk": case.pk}
        )

    def test_the_form_says_which_session_it_is_for(
        self, client, sign_in, couple_case, make_booking
    ):
        case, ada, _ben = couple_case
        booking = make_booking(case, ada)
        sign_in(ada)

        page = client.get(f"{upload_url(case)}?booking={booking.pk}").content.decode()

        assert "For your appointment on" in page
        # The hidden field is what carries it through the POST; without it the link
        # would be lost the moment the form is submitted.
        assert f'name="booking" value="{booking.pk}"' in page

    def test_a_counselor_can_send_a_handout_for_a_session_too(
        self, client, sign_in, couple_case, counselor, make_booking
    ):
        """Both directions, as the case-wide upload has always been."""
        case, ada, _ben = couple_case
        booking = make_booking(case, ada)
        sign_in(counselor)

        client.post(
            upload_url(case),
            upload_payload(
                name="reading.pdf",
                kind="handout",
                # The counselor's form has this field; a counselee's does not.
                visibility=Visibility.CASE_SHARED,
                booking=booking.pk,
            ),
        )

        document = Document.objects.get()
        assert document.booking_id == booking.pk
        assert document.visibility == Visibility.CASE_SHARED


class TestTheLinkIsNotARoute:
    def test_an_appointment_on_another_case_files_nothing(
        self, client, sign_in, couple_case, counselor, make_booking
    ):
        """A booking id the uploader can legitimately see, on the wrong case.

        Ignored rather than a 404: the link is a label, and the file is what the
        counselee came to send. But it is not written, because a document carrying
        another case's appointment would appear on that case's session page.
        """
        case, ada, _ben = couple_case
        other_case = Case.objects.create(counselor=counselor, label="Ashford — individual")
        CaseMember.objects.create(case=other_case, counselee=ada)
        elsewhere = make_booking(other_case, ada)
        sign_in(ada)

        client.post(upload_url(case), upload_payload(booking=elsewhere.pk))

        document = Document.objects.get()
        assert document.case_id == case.pk
        assert document.booking_id is None

    def test_a_spouses_own_appointment_cannot_be_filed_against(
        self, client, sign_in, couple_case, make_booking
    ):
        """Ada may not attach anything to Ben's individual session.

        She cannot see it — ``for_actor`` excludes it — and this asserts that the
        query string is no way around that, since a document filed against Ben's
        appointment would tell him she had one to send.
        """
        case, ada, ben = couple_case
        bens = make_booking(case, ben)
        sign_in(ada)

        client.post(upload_url(case), upload_payload(booking=bens.pk))

        assert Document.objects.get().booking_id is None

    def test_a_meaningless_id_loses_the_link_and_keeps_the_file(self, client, sign_in, couple_case):
        case, ada, _ben = couple_case
        sign_in(ada)

        response = client.post(upload_url(case), upload_payload(booking="not-a-number"))

        assert response.status_code == 302
        assert Document.objects.get().booking_id is None


class TestWhatTheSessionPageShows:
    def test_the_counselor_sees_what_was_sent_in_for_it(
        self, client, sign_in, couple_case, counselor, make_booking
    ):
        case, ada, _ben = couple_case
        booking = make_booking(case, ada)
        sign_in(ada)
        client.post(upload_url(case), upload_payload(name="week-one.pdf", booking=booking.pk))
        client.logout()
        sign_in(counselor)

        page = client.get(reverse("scheduling:detail", args=[booking.pk])).content.decode()

        assert "week-one.pdf" in page

    def test_the_other_counselee_on_a_joint_session_does_not(
        self, client, sign_in, couple_case, make_booking
    ):
        """The promise, on the newest surface.

        A joint appointment is visible to everyone expected at it, so Ben can open
        this page. What Ada handed in privately is still hers.
        """
        case, ada, ben = couple_case
        booking = make_booking(case, ada, attendance=Attendance.WHOLE_CASE)
        sign_in(ada)
        client.post(upload_url(case), upload_payload(name="ada-only.pdf", booking=booking.pk))
        client.logout()
        sign_in(ben)

        page = client.get(reverse("scheduling:detail", args=[booking.pk]))

        assert page.status_code == 200
        assert "ada-only.pdf" not in page.content.decode()

    def test_billing_is_shown_no_documents_at_all(
        self, client, sign_in, couple_case, financial_admin, make_booking
    ):
        """A financial administrator opens this page to invoice the session.

        ``Document.objects.for_actor`` is ``none()`` for them, so the block is not
        rendered — not even its heading, because "Documents (none)" would be a
        statement about content.
        """
        case, ada, _ben = couple_case
        booking = make_booking(case, ada)
        sign_in(ada)
        client.post(upload_url(case), upload_payload(name="disclosure.pdf", booking=booking.pk))
        client.logout()
        sign_in(financial_admin)

        page = client.get(reverse("scheduling:detail", args=[booking.pk]))

        assert page.status_code == 200
        body = page.content.decode()
        assert "disclosure.pdf" not in body
        assert "Documents for this session" not in body


class TestWhatTheDiaryOffers:
    def test_a_counselee_is_offered_an_upload_beside_each_appointment(
        self, client, sign_in, couple_case, make_booking
    ):
        case, ada, _ben = couple_case
        upcoming = make_booking(case, ada)
        past = make_booking(case, ada, days=-7)
        sign_in(ada)

        page = client.get(reverse("scheduling:appointments")).content.decode()

        # Past as well as future: homework is handed in after the meeting more
        # often than before it.
        assert f"{upload_url(case)}?booking={upcoming.pk}" in page
        assert f"{upload_url(case)}?booking={past.pk}" in page

    def test_the_counselor_is_offered_it_too(
        self, client, sign_in, couple_case, counselor, make_booking
    ):
        case, ada, _ben = couple_case
        booking = make_booking(case, ada)
        sign_in(counselor)

        page = client.get(reverse("scheduling:appointments")).content.decode()

        assert f"{upload_url(case)}?booking={booking.pk}" in page
