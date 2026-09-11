"""
Who can reach which appointment.

tests/test_access_matrix.py asks whether each role may reach each route with a
booking it is legitimately connected to. This file asks what happens when it is
not, and it exists for three promises that the matrix cannot state:

  * **On a shared case, one member's appointment is not the other's business.**
    A family case's Thursday afternoon belongs to whoever booked it. A joint
    session is different, and the difference is ``Booking.attendance``.
  * **financial_admin may see that a session happened and nothing about what was
    said.** This is where scheduling diverges from documents on purpose, so the
    divergence is asserted rather than assumed: billing reads a case's sessions,
    is refused the ministry-wide diary, and never sees a note.
  * **Whose time is taken is never disclosed by the booking page.** A slot
    vanishing is all a counselee learns.

Everything goes through the real views. A test asserting on a hand-built queryset
would pass while the view called ``.objects.all()``.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import Case, CaseMember, CounselorProfile
from apps.scheduling.models import (
    Attendance,
    AvailabilityRule,
    Booking,
    BookingStatus,
    Weekday,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def family(counselor, make_user):
    """A shared case: one counselor, two counselees who share a surname.

    The shape that makes isolation non-trivial, so it is the default here. Given
    names differ so an assertion about what a page shows can tell the two apart.
    """
    CounselorProfile.objects.create(user=counselor)
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage", kind="couple")
    ada = make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")
    ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=ada)
    CaseMember.objects.create(case=case, counselee=ben)
    return case, ada, ben


def make_booking(case, counselee, *, hours_ahead=72, status=BookingStatus.CONFIRMED, **kwargs):
    start = timezone.now() + timedelta(hours=hours_ahead)
    return Booking.objects.create(
        counselor=case.counselor,
        case=case,
        counselee=counselee,
        slot=Booking.range_for(start, 60),
        status=status,
        created_by=counselee,
        **kwargs,
    )


def get(client, name, **kwargs):
    return client.get(reverse(f"scheduling:{name}", kwargs=kwargs))


def post(client, name, data=None, **kwargs):
    return client.post(reverse(f"scheduling:{name}", kwargs=kwargs), data or {})


# --- one shared case, two counselees --------------------------------------


class TestCounseleesOnASharedCase:
    def test_one_members_appointment_is_invisible_to_the_other(self, family, client, sign_in):
        """404, not 403. A refusal would confirm that Ben has a Thursday afternoon,
        which on a marriage case is the disclosure."""
        case, ada, ben = family
        hers = make_booking(case, ada)
        sign_in(ben)

        assert get(client, "detail", public_id=hers.public_id).status_code == 404

    def test_a_joint_appointment_is_visible_to_everyone_expected_at_it(
        self, family, client, sign_in
    ):
        case, ada, ben = family
        joint = make_booking(case, ada, attendance=Attendance.WHOLE_CASE)
        sign_in(ben)

        assert get(client, "detail", public_id=joint.public_id).status_code == 200

    def test_a_joint_appointment_does_not_name_who_arranged_it(self, family, client, sign_in):
        """``Booking.counselee`` on a joint booking is whoever arranged it, and on a
        family case that is not a fact to publish to the rest of the case."""
        case, ada, ben = family
        joint = make_booking(case, ada, attendance=Attendance.WHOLE_CASE)
        sign_in(ben)

        body = get(client, "detail", public_id=joint.public_id).content.decode()

        assert "Ada" not in body
        assert "Everyone on the case" in body

    def test_a_counselees_own_appointment_says_so(self, family, client, sign_in):
        case, ada, _ = family
        hers = make_booking(case, ada)
        sign_in(ada)

        assert "You" in get(client, "detail", public_id=hers.public_id).content.decode()

    def test_the_diary_holds_only_what_the_viewer_may_see(self, family, client, sign_in):
        case, ada, ben = family
        hers = make_booking(case, ada, hours_ahead=48)
        his = make_booking(case, ben, hours_ahead=72)
        joint = make_booking(case, ada, hours_ahead=96, attendance=Attendance.WHOLE_CASE)
        sign_in(ben)

        upcoming = get(client, "appointments").context["upcoming"]

        assert set(upcoming) == {his, joint}
        assert hers not in upcoming

    def test_a_case_appointment_list_is_filtered_the_same_way(self, family, client, sign_in):
        """Same scoping, different route. The case page must not be the way round it."""
        case, ada, ben = family
        hers = make_booking(case, ada)
        sign_in(ben)

        upcoming = get(client, "case_appointments", case_public_id=case.public_id).context[
            "upcoming"
        ]

        assert hers not in upcoming

    def test_a_counselee_cannot_cancel_the_others_appointment(self, family, client, sign_in):
        case, ada, ben = family
        hers = make_booking(case, ada)
        sign_in(ben)

        assert get(client, "cancel", public_id=hers.public_id).status_code == 404
        assert post(client, "cancel", {"reason": ""}, public_id=hers.public_id).status_code == 404
        hers.refresh_from_db()
        assert hers.status == BookingStatus.CONFIRMED

    def test_a_counselee_may_cancel_their_own(self, family, client, sign_in):
        """The point of self-booking. Cancelling early is what frees the time up."""
        case, ada, _ = family
        hers = make_booking(case, ada)
        sign_in(ada)

        response = post(client, "cancel", {"reason": "Away that week."}, public_id=hers.public_id)

        assert response.status_code == 302
        hers.refresh_from_db()
        assert hers.status == BookingStatus.CANCELLED
        assert hers.cancelled_by == ada


# --- the counselee is not staff -------------------------------------------


class TestWhatACounseleeMayNotDo:
    def test_a_counselee_cannot_confirm_their_own_request(self, family, client, sign_in):
        """Otherwise ``requested`` would mean nothing and the counselor's agreement
        would be a formality the counselee could grant themselves."""
        case, ada, _ = family
        hers = make_booking(case, ada, status=BookingStatus.REQUESTED)
        sign_in(ada)

        assert post(client, "confirm", public_id=hers.public_id).status_code == 403
        hers.refresh_from_db()
        assert hers.status == BookingStatus.REQUESTED

    def test_a_counselee_cannot_reschedule(self, family, client, sign_in):
        """Rescheduling bypasses the office hours, so it is not theirs to do. Their
        route is to cancel and book again from what is offered."""
        case, ada, _ = family
        hers = make_booking(case, ada)
        sign_in(ada)

        assert get(client, "reschedule", public_id=hers.public_id).status_code == 403

    def test_a_counselee_cannot_record_an_outcome(self, family, client, sign_in):
        case, ada, _ = family
        held = make_booking(case, ada, hours_ahead=-72)
        sign_in(ada)

        assert get(client, "outcome", public_id=held.public_id).status_code == 403

    def test_a_counselee_never_sees_the_session_note(self, family, client, sign_in):
        case, ada, _ = family
        held = make_booking(
            case,
            ada,
            hours_ahead=-72,
            status=BookingStatus.COMPLETED,
            counselor_note="Ada disclosed a history of self-harm.",
        )
        sign_in(ada)

        response = get(client, "detail", public_id=held.public_id)

        assert response.status_code == 200
        assert response.context["show_notes"] is False
        assert "self-harm" not in response.content.decode()

    def test_a_counselee_cannot_touch_a_counselors_office_hours(self, family, client, sign_in):
        """The scoping queryset lets them *read* the hours of a counselor they book
        with, which is why the view filters on ``counselor=request.user`` as well."""
        case, ada, _ = family
        rule = AvailabilityRule.objects.create(
            counselor=case.counselor,
            weekday=Weekday.TUESDAY,
            start_time="09:00",
            end_time="12:00",
        )
        sign_in(ada)

        assert post(client, "availability_delete", public_id=rule.public_id).status_code == 404
        assert AvailabilityRule.objects.filter(pk=rule.pk).exists()


# --- the session note -----------------------------------------------------


class TestTheSessionNote:
    """Writing a note, which is a narrower permission than reading one.

    ``scheduling.view_booking_note`` covers the counselor and an admin;
    ``scheduling.change_booking_note`` covers the counselor alone. An admin
    correcting a billing record is an administrative act; an admin authoring what
    was said in a room they were not in is not, and the asymmetry is the point of
    having two permissions rather than one.
    """

    def test_a_counselor_may_note_an_appointment_that_has_not_happened(
        self, family, client, sign_in
    ):
        """The gap this route exists for. ``outcome`` refuses a future booking, so
        without it there is nowhere to write "follow up on last week's homework"."""
        case, ada, _ = family
        upcoming = make_booking(case, ada)
        sign_in(case.counselor)

        assert get(client, "outcome", public_id=upcoming.public_id).status_code == 403
        assert get(client, "note", public_id=upcoming.public_id).status_code == 200

        response = post(
            client,
            "note",
            {"counselor_note": "Follow up on the Ephesians homework."},
            public_id=upcoming.public_id,
        )

        assert response.status_code == 302
        upcoming.refresh_from_db()
        assert upcoming.counselor_note == "Follow up on the Ephesians homework."
        # Untouched: noting an appointment is not closing it out.
        assert upcoming.status == BookingStatus.CONFIRMED

    def test_amending_a_note_does_not_require_re_answering_what_happened(
        self, family, client, sign_in
    ):
        case, ada, _ = family
        held = make_booking(
            case,
            ada,
            hours_ahead=-72,
            status=BookingStatus.COMPLETED,
            counselor_note="First draft.",
        )
        sign_in(case.counselor)

        post(
            client, "note", {"counselor_note": "Corrected on reflection."}, public_id=held.public_id
        )

        held.refresh_from_db()
        assert held.counselor_note == "Corrected on reflection."
        assert held.status == BookingStatus.COMPLETED

    def test_writing_a_note_is_audited_without_recording_the_note(self, family, client, sign_in):
        """The trail is read by people who may not read the note itself, so it
        carries the length and nothing else. A note quoted into an audit row would
        be counseling content in a table with a wider audience."""
        case, ada, _ = family
        upcoming = make_booking(case, ada)
        sign_in(case.counselor)

        post(
            client,
            "note",
            {"counselor_note": "Ada disclosed a history of self-harm."},
            public_id=upcoming.public_id,
        )

        event = AuditEvent.objects.filter(verb=AuditVerb.BOOKING_NOTE_UPDATED).get()
        assert event.actor == case.counselor
        assert event.target_id == str(upcoming.pk)
        assert "self-harm" not in str(event.metadata)
        assert event.metadata["length"] == len("Ada disclosed a history of self-harm.")

    def test_an_admin_may_read_a_note_but_not_write_one(self, family, client, sign_in, admin_user):
        case, ada, _ = family
        held = make_booking(
            case,
            ada,
            hours_ahead=-72,
            status=BookingStatus.COMPLETED,
            counselor_note="Ada disclosed a history of self-harm.",
        )
        sign_in(admin_user)

        detail = get(client, "detail", public_id=held.public_id)

        assert detail.context["show_notes"] is True
        assert detail.context["can_edit_note"] is False
        # And no link offering an action that would only 403.
        assert (
            reverse("scheduling:note", kwargs={"public_id": held.public_id})
            not in detail.content.decode()
        )
        assert get(client, "note", public_id=held.public_id).status_code == 403
        assert (
            post(
                client, "note", {"counselor_note": "Rewritten."}, public_id=held.public_id
            ).status_code
            == 403
        )
        held.refresh_from_db()
        assert held.counselor_note == "Ada disclosed a history of self-harm."

    def test_the_counselor_is_offered_the_note_from_the_appointment(self, family, client, sign_in):
        case, ada, _ = family
        upcoming = make_booking(case, ada)
        sign_in(case.counselor)

        detail = get(client, "detail", public_id=upcoming.public_id)

        assert detail.context["can_edit_note"] is True
        assert (
            reverse("scheduling:note", kwargs={"public_id": upcoming.public_id})
            in detail.content.decode()
        )

    def test_a_counselee_cannot_write_a_note_about_themselves(self, family, client, sign_in):
        case, ada, _ = family
        upcoming = make_booking(case, ada)
        sign_in(ada)

        assert get(client, "note", public_id=upcoming.public_id).status_code == 403
        assert (
            post(
                client, "note", {"counselor_note": "Say I did well."}, public_id=upcoming.public_id
            ).status_code
            == 403
        )
        upcoming.refresh_from_db()
        assert upcoming.counselor_note == ""

    def test_another_counselors_note_is_a_404(self, family, client, sign_in, other_counselor):
        """404 rather than 403: the booking is not in ``for_actor`` at all, and a
        refusal would confirm that this counselor has an appointment on that day."""
        case, ada, _ = family
        upcoming = make_booking(case, ada)
        sign_in(other_counselor)

        assert get(client, "note", public_id=upcoming.public_id).status_code == 404


# --- financial_admin ------------------------------------------------------


class TestFinancialAdmin:
    """The role the separation exists for, and the one place it is not a blanket no.

    Read against tests/test_documents_access.py, where every answer is a refusal.
    Here billing may read a session, because a session is what gets invoiced.
    """

    def test_billing_is_refused_the_ministry_wide_diary(
        self, family, client, sign_in, financial_admin
    ):
        """403 rather than an empty page: a diary is caseload-shaped, and who is
        seeing a counselor this week is not billing data. Sessions come per case."""
        sign_in(financial_admin)

        assert get(client, "appointments").status_code == 403

    def test_billing_may_read_a_cases_sessions(self, family, client, sign_in, financial_admin):
        case, ada, _ = family
        held = make_booking(case, ada, hours_ahead=-72, status=BookingStatus.COMPLETED)
        sign_in(financial_admin)

        response = get(client, "case_appointments", case_public_id=case.public_id)

        assert response.status_code == 200
        assert held in response.context["past"]

    def test_billing_sees_no_note_on_a_session_it_may_read(
        self, family, client, sign_in, financial_admin
    ):
        case, ada, _ = family
        held = make_booking(
            case,
            ada,
            hours_ahead=-72,
            status=BookingStatus.COMPLETED,
            request_note="I think my marriage is over.",
            counselor_note="Ada disclosed a history of self-harm.",
        )
        sign_in(financial_admin)

        response = get(client, "detail", public_id=held.public_id)

        assert response.status_code == 200
        assert response.context["show_notes"] is False
        body = response.content.decode()
        assert "marriage is over" not in body
        assert "self-harm" not in body

    def test_a_case_session_list_carries_no_note_either(
        self, family, client, sign_in, financial_admin
    ):
        """The table billing actually works from. When, who, how long, what became
        of it — and nothing else."""
        case, ada, _ = family
        make_booking(
            case,
            ada,
            hours_ahead=-72,
            status=BookingStatus.COMPLETED,
            request_note="I think my marriage is over.",
            counselor_note="Ada disclosed a history of self-harm.",
        )
        sign_in(financial_admin)

        body = get(client, "case_appointments", case_public_id=case.public_id).content.decode()

        assert "marriage is over" not in body
        assert "self-harm" not in body

    def test_billing_cannot_book_or_schedule(self, family, client, sign_in, financial_admin):
        case, _, _ = family
        sign_in(financial_admin)

        assert get(client, "book", case_public_id=case.public_id).status_code == 403
        assert get(client, "schedule", case_public_id=case.public_id).status_code == 403

    def test_billing_cannot_act_on_an_appointment_it_can_read(
        self, family, client, sign_in, financial_admin
    ):
        case, ada, _ = family
        hers = make_booking(case, ada, status=BookingStatus.REQUESTED)
        sign_in(financial_admin)

        assert post(client, "confirm", public_id=hers.public_id).status_code == 403
        assert get(client, "cancel", public_id=hers.public_id).status_code == 403
        assert get(client, "reschedule", public_id=hers.public_id).status_code == 403
        hers.refresh_from_db()
        assert hers.status == BookingStatus.REQUESTED

    def test_billing_has_no_office_hours_of_its_own(self, client, sign_in, financial_admin):
        sign_in(financial_admin)

        assert get(client, "availability").status_code == 403

    def test_a_late_cancellation_is_visible_to_billing(
        self, family, client, sign_in, financial_admin
    ):
        """The one billing-relevant fact a cancellation carries, and the reason
        ``was_late_cancellation`` is stored rather than derived."""
        case, ada, _ = family
        cancelled = make_booking(
            case,
            ada,
            hours_ahead=-72,
            status=BookingStatus.CANCELLED,
            cancelled_at=timezone.now(),
            was_late_cancellation=True,
        )
        sign_in(financial_admin)

        body = get(client, "case_appointments", case_public_id=case.public_id).content.decode()

        assert (
            cancelled
            in get(client, "case_appointments", case_public_id=case.public_id).context["past"]
        )
        assert "late notice" in body


# --- across counselors ----------------------------------------------------


class TestAcrossCounselors:
    def test_a_counselor_cannot_see_another_counselors_appointment(
        self, family, client, sign_in, other_counselor
    ):
        case, ada, _ = family
        hers = make_booking(case, ada)
        sign_in(other_counselor)

        assert get(client, "detail", public_id=hers.public_id).status_code == 404

    def test_a_counselor_cannot_delete_another_counselors_office_hours(
        self, family, client, sign_in, other_counselor
    ):
        case, _, _ = family
        rule = AvailabilityRule.objects.create(
            counselor=case.counselor,
            weekday=Weekday.TUESDAY,
            start_time="09:00",
            end_time="12:00",
        )
        sign_in(other_counselor)

        assert post(client, "availability_delete", public_id=rule.public_id).status_code == 404
        assert AvailabilityRule.objects.filter(pk=rule.pk).exists()

    def test_the_office_hours_page_shows_only_the_viewers_own(
        self, family, client, sign_in, other_counselor
    ):
        case, _, _ = family
        theirs = AvailabilityRule.objects.create(
            counselor=case.counselor,
            weekday=Weekday.TUESDAY,
            start_time="09:00",
            end_time="12:00",
        )
        mine = AvailabilityRule.objects.create(
            counselor=other_counselor,
            weekday=Weekday.WEDNESDAY,
            start_time="13:00",
            end_time="15:00",
        )
        sign_in(other_counselor)

        rules = get(client, "availability").context["rules"]

        assert list(rules) == [mine]
        assert theirs not in rules


# --- the booking page -----------------------------------------------------


class TestTheBookingPage:
    def test_a_taken_slot_disappears_without_saying_whose_it_is(self, family, client, sign_in):
        """All a counselee learns is that the time is gone.

        The busy list is read unscoped — the one place in the application that does
        — because otherwise Ada would be offered Ben's Thursday and the constraint
        would refuse it with no explanation. Only the interval leaves the query.
        """
        case, ada, ben = family
        for weekday in Weekday.values:
            AvailabilityRule.objects.create(
                counselor=case.counselor, weekday=weekday, start_time="09:00", end_time="17:00"
            )
        sign_in(ada)

        before = get(client, "book", case_public_id=case.public_id)
        first_day, day_slots = before.context["days"][0]
        taken = day_slots[0]
        make_booking(
            case,
            ben,
            hours_ahead=(taken.start - timezone.now()).total_seconds() / 3600,
        )

        after = get(client, "book", case_public_id=case.public_id)
        still_offered = [slot for _, slots in after.context["days"] for slot in slots]

        assert taken not in still_offered
        assert "Ben" not in after.content.decode()

    def test_a_counselee_books_a_time_in_two_steps(self, family, client, sign_in):
        """The whole no-JavaScript flow: list, pick, confirm.

        Two GETs and a POST rather than a grid of forms, because the CSP has no
        'unsafe-inline' and a page carrying one form per slot would repeat the note
        field forty times over.
        """
        case, ada, _ = family
        for weekday in Weekday.values:
            AvailabilityRule.objects.create(
                counselor=case.counselor, weekday=weekday, start_time="09:00", end_time="17:00"
            )
        sign_in(ada)

        listing = get(client, "book", case_public_id=case.public_id)
        assert listing.context["form"] is None, "no form until a time is picked"
        slot = listing.context["days"][0][1][0]

        url = reverse("scheduling:book", kwargs={"case_public_id": case.public_id})
        picked = client.get(url, {"slot": slot.start.isoformat()})
        assert picked.context["form"] is not None
        assert picked.context["chosen"] == slot.start

        confirmed = client.post(
            url,
            {
                "slot": slot.start.isoformat(),
                "attendance": Attendance.INDIVIDUAL,
                "request_note": "Struggling with the homework.",
            },
        )

        booking = Booking.objects.get()
        assert confirmed.status_code == 302
        assert confirmed["Location"] == reverse(
            "scheduling:detail", kwargs={"public_id": booking.public_id}
        )
        assert booking.counselee == ada
        assert booking.starts_at == slot.start
        # Requested, not confirmed: the counselor still has to agree.
        assert booking.status == BookingStatus.REQUESTED
        assert booking.request_note == "Struggling with the homework."

    def test_posting_a_slot_that_is_no_longer_offered_is_refused_gracefully(
        self, family, client, sign_in
    ):
        """The page sat open while somebody else took the time.

        Re-rendered with the message and a fresh list rather than a 500, and the
        slot that went is simply not in it the second time.
        """
        case, ada, ben = family
        for weekday in Weekday.values:
            AvailabilityRule.objects.create(
                counselor=case.counselor, weekday=weekday, start_time="09:00", end_time="17:00"
            )
        sign_in(ada)

        url = reverse("scheduling:book", kwargs={"case_public_id": case.public_id})
        slot = get(client, "book", case_public_id=case.public_id).context["days"][0][1][0]
        make_booking(case, ben, hours_ahead=(slot.start - timezone.now()).total_seconds() / 3600)

        response = client.post(
            url,
            {
                "slot": slot.start.isoformat(),
                "attendance": Attendance.INDIVIDUAL,
                "request_note": "",
            },
        )

        assert response.status_code == 200
        assert Booking.objects.filter(counselee=ada).count() == 0
        assert response.context["form"].non_field_errors()
        assert slot not in [s for _, slots in response.context["days"] for s in slots]

    def test_a_counselee_can_only_book_on_their_own_case(self, family, client, sign_in, make_user):
        case, ada, _ = family
        stranger = make_user(Role.COUNSELEE)
        sign_in(stranger)

        # 404 rather than 403: the case itself is not in their scope, so its
        # existence is what would be disclosed.
        assert get(client, "book", case_public_id=case.public_id).status_code == 404

    def test_a_counselor_sees_the_page_but_cannot_post_to_it(self, family, client, sign_in):
        """They are looking at what their counselee would see. A counselor cannot be
        their own counselee, and their route is ``schedule``."""
        case, _, _ = family
        for weekday in Weekday.values:
            AvailabilityRule.objects.create(
                counselor=case.counselor, weekday=weekday, start_time="09:00", end_time="17:00"
            )
        sign_in(case.counselor)

        page = get(client, "book", case_public_id=case.public_id)
        assert page.status_code == 200
        assert page.context["may_book"] is False

        slot = page.context["days"][0][1][0]
        refused = client.post(
            reverse("scheduling:book", kwargs={"case_public_id": case.public_id}),
            {"slot": slot.start.isoformat(), "request_note": ""},
        )
        assert refused.status_code == 403
        assert Booking.objects.count() == 0


# --- the trail ------------------------------------------------------------


def test_a_refused_action_is_recorded(family, client, sign_in):
    """A counselee trying to confirm their own request is worth a line in the trail.

    Not the 404s: an object that is not in ``for_actor`` never reaches a
    permission check, and auditing every mistyped URL would bury the attempts that
    mean something.
    """
    case, ada, _ = family
    hers = make_booking(case, ada, status=BookingStatus.REQUESTED)
    sign_in(ada)

    post(client, "confirm", public_id=hers.public_id)

    event = AuditEvent.objects.get(verb=AuditVerb.ACCESS_DENIED)
    assert event.actor == ada
    assert event.actor_role == Role.COUNSELEE
    assert event.metadata["permission"] == "scheduling.confirm_booking"
