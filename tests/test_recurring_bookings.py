"""
Weekly series, and the link that makes a session virtual.

Three promises, and each of them is a thing that would be quietly wrong rather
than loudly broken:

  * **A series is N appointments, not one repeating event.** Each week can be
    moved, cancelled, noted and billed on its own, and they share a key so a
    counselor can act on the rest of the arrangement. A test here would pass
    against a design that stored a repeat rule, so what is asserted is that the
    rows exist and that touching one leaves the others alone.
  * **"Tuesdays at two" stays two o'clock.** Adding seven days to an instant
    drifts by an hour when the clocks change, and the counselee is the one who
    would notice. The series is built from dates and a local time instead.
  * **A meeting link is a way into a counseling session, and is treated like
    one.** Only the counselor sets it, never an administrator; the audit trail
    records that a session became virtual and never the link itself.

tests/test_booking.py covers a single booking. tests/test_access_matrix.py covers
who may reach the meeting-link page at all.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import Case, CaseMember, CounselorProfile
from apps.scheduling import services
from apps.scheduling.forms import RecurringBookingForm
from apps.scheduling.models import Attendance, Booking, BookingStatus, Weekday

pytestmark = pytest.mark.django_db


@pytest.fixture
def practice(counselor, counselee):
    """One counselor, one case, one counselee on it. No office hours needed.

    A counselor's own booking is not bound by the published hours, and a series is
    a counselor's booking, so arranging availability would be arranging something
    the code under test does not read.
    """
    CounselorProfile.objects.create(user=counselor)
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    CaseMember.objects.create(case=case, counselee=counselee)
    return SimpleNamespace(case=case, counselor=counselor, counselee=counselee)


def next_weekday(weekday: int, *, zone, hour=14):
    """The next occurrence of ``weekday`` at ``hour``, in ``zone``. Always ahead."""
    today = timezone.now().astimezone(zone).date()
    day = today + timedelta(days=(weekday - today.weekday()) % 7 or 7)
    return datetime(day.year, day.month, day.day, hour, tzinfo=zone)


def next_clock_change(zone):
    """The next date in ``zone`` whose UTC offset differs from the day before it.

    Found by walking days rather than by naming one, so the test that uses it keeps
    working when the tzdata in the image changes, when the rule changes, and next
    year. Noon is used for the comparison because that is never itself inside the
    ambiguous or missing hour.
    """
    day = timezone.now().astimezone(zone).date() + timedelta(days=1)
    for _ in range(400):
        before = datetime(day.year, day.month, day.day, 12, tzinfo=zone) - timedelta(days=1)
        after = datetime(day.year, day.month, day.day, 12, tzinfo=zone)
        if before.utcoffset() != after.utcoffset():
            return day
        day += timedelta(days=1)
    raise AssertionError(f"{zone} appears not to observe daylight saving")


def make_series(practice, *, occurrences=4, start=None, **kwargs):
    zone = practice.counselor.zoneinfo
    return services.book_series(
        case=practice.case,
        counselee=practice.counselee,
        start=start or next_weekday(Weekday.TUESDAY, zone=zone),
        occurrences=occurrences,
        minutes=60,
        created_by=practice.counselor,
        **kwargs,
    )


# --- what a series is -----------------------------------------------------


class TestBookingASeries:
    def test_every_week_is_its_own_appointment(self, practice):
        series = make_series(practice, occurrences=6)

        assert len(series) == 6
        assert Booking.objects.filter(case=practice.case).count() == 6

    def test_they_are_a_week_apart_at_the_same_time(self, practice):
        zone = practice.counselor.zoneinfo
        series = make_series(practice, occurrences=5)
        starts = [b.starts_at.astimezone(zone) for b in series.bookings]

        assert [s.time() for s in starts] == [starts[0].time()] * 5
        assert [(s.date() - starts[0].date()).days for s in starts] == [0, 7, 14, 21, 28]

    def test_they_share_one_key_and_nothing_else_does(self, practice):
        """The key is what lets a counselor act on the rest of the arrangement."""
        series = make_series(practice, occurrences=3)
        keys = {booking.series_key for booking in series.bookings}

        assert len(keys) == 1
        assert keys != {None}
        assert set(series.first.series_appointments()) == set(series.bookings)

    def test_a_single_booking_is_in_no_series(self, practice):
        booking = services.book(
            case=practice.case,
            counselee=practice.counselee,
            start=next_weekday(Weekday.TUESDAY, zone=practice.counselor.zoneinfo),
            minutes=60,
            created_by=practice.counselor,
            enforce_availability=False,
        )

        assert booking.series_key is None
        assert list(booking.series_appointments()) == []

    def test_a_counselors_series_is_confirmed_straight_away(self, practice):
        """Same rule as a single booking: staff scheduling it have already decided."""
        assert {b.status for b in make_series(practice).bookings} == {BookingStatus.CONFIRMED}

    def test_cancelling_one_week_leaves_the_others_standing(self, practice):
        series = make_series(practice, occurrences=4)

        services.cancel(series.bookings[1], actor=practice.counselor, reason="Away")

        statuses = [Booking.objects.get(pk=b.pk).status for b in series.bookings]
        assert statuses == [
            BookingStatus.CONFIRMED,
            BookingStatus.CANCELLED,
            BookingStatus.CONFIRMED,
            BookingStatus.CONFIRMED,
        ]

    def test_moving_one_week_does_not_move_the_rest(self, practice):
        series = make_series(practice, occurrences=3)
        second = series.bookings[1]
        moved_to = second.starts_at + timedelta(days=2)

        services.reschedule(second, actor=practice.counselor, start=moved_to)

        third = series.bookings[2]
        assert Booking.objects.get(pk=second.pk).starts_at == moved_to
        assert Booking.objects.get(pk=third.pk).starts_at == third.starts_at
        # Still one of the arrangement, which is the point of a key over a rule.
        assert Booking.objects.get(pk=second.pk).series_key == series.first.series_key


class TestWhenAWeekIsAlreadyTaken:
    def test_the_clashing_week_is_skipped_and_the_rest_are_booked(self, practice):
        """Refusing ten because of one would make the feature unusable on a real diary."""
        zone = practice.counselor.zoneinfo
        start = next_weekday(Weekday.TUESDAY, zone=zone)
        services.book(
            case=practice.case,
            counselee=practice.counselee,
            start=start + timedelta(weeks=2),
            minutes=60,
            created_by=practice.counselor,
            enforce_availability=False,
        )

        series = make_series(practice, occurrences=4, start=start)

        assert len(series) == 3
        assert [day for day, _reason in series.skipped] == [
            (start + timedelta(weeks=2)).astimezone(zone).date()
        ]

    def test_a_series_that_could_not_book_anything_is_a_refusal(self, practice):
        zone = practice.counselor.zoneinfo
        start = next_weekday(Weekday.TUESDAY, zone=zone)
        services.book(
            case=practice.case,
            counselee=practice.counselee,
            start=start,
            minutes=60,
            created_by=practice.counselor,
            enforce_availability=False,
        )

        with pytest.raises(services.SchedulingError):
            make_series(practice, occurrences=1, start=start)

    def test_nothing_is_written_when_the_only_week_clashes(self, practice):
        zone = practice.counselor.zoneinfo
        start = next_weekday(Weekday.TUESDAY, zone=zone)
        services.book(
            case=practice.case,
            counselee=practice.counselee,
            start=start,
            minutes=60,
            created_by=practice.counselor,
            enforce_availability=False,
        )

        with pytest.raises(services.SchedulingError):
            make_series(practice, occurrences=1, start=start)

        assert Booking.objects.filter(case=practice.case).count() == 1

    def test_an_absurd_number_of_sessions_is_refused(self, practice):
        with pytest.raises(services.SchedulingError):
            make_series(practice, occurrences=500)

        assert Booking.objects.filter(case=practice.case).count() == 0


class TestTheClocksChanging:
    def test_the_hour_is_the_same_every_week_across_a_transition(self, practice):
        """The reason each week is rebuilt from a date rather than by adding seven days.

        US daylight saving ends on the first Sunday of November, so a series that
        starts a fortnight before it and runs a fortnight past it spans the
        transition. Every week has to still be two o'clock in the afternoon to the
        people attending, which means the UTC instant has to shift by an hour and
        the wall clock has to not.

        The transition is worked out from the zone rather than written down, because
        a fixed date in a test is a date that arrives, and an appointment in the
        past is refused for reasons that have nothing to do with this.
        """
        zone = ZoneInfo("America/New_York")
        practice.counselor.timezone_name = "America/New_York"
        practice.counselor.save(update_fields=["timezone_name"])
        transition = next_clock_change(zone)
        # The Tuesday a fortnight before it, so two sessions fall either side.
        start = datetime(transition.year, transition.month, transition.day, 14, tzinfo=zone)
        start -= timedelta(days=(start.weekday() - Weekday.TUESDAY) % 7 + 14)

        series = make_series(practice, occurrences=5, start=start)

        local = [b.starts_at.astimezone(zone) for b in series.bookings]
        assert [t.hour for t in local] == [14] * 5
        assert [(t.date() - start.date()).days for t in local] == [0, 7, 14, 21, 28]
        # The proof that the wall clock was preserved rather than the instant: the
        # offset changed under it.
        assert local[0].utcoffset() != local[-1].utcoffset()


class TestTheEmailAboutASeries:
    def test_one_email_covers_the_whole_series(self, practice):
        """Ten messages for one decision is mail people learn to filter."""
        mail.outbox.clear()

        make_series(practice, occurrences=6)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [practice.counselee.email]

    def test_it_lists_every_session(self, practice):
        mail.outbox.clear()

        series = make_series(practice, occurrences=3)

        body = mail.outbox[0].body
        zone = practice.counselee.zoneinfo
        for booking in series.bookings:
            assert booking.starts_at.astimezone(zone).strftime("%d %B") in body

    def test_it_names_no_case(self, practice):
        """The same rule as every other appointment email: no case label in mail."""
        mail.outbox.clear()

        make_series(practice)

        assert "Ashford" not in mail.outbox[0].body
        assert "Ashford" not in mail.outbox[0].subject

    def test_a_joint_series_reaches_everyone_on_the_case_once(self, practice, make_user):
        ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
        CaseMember.objects.create(case=practice.case, counselee=ben)
        mail.outbox.clear()

        make_series(practice, occurrences=4, attendance=Attendance.WHOLE_CASE)

        assert len(mail.outbox) == 2
        assert {address for message in mail.outbox for address in message.to} == {
            practice.counselee.email,
            ben.email,
        }


class TestTheTrail:
    def test_every_appointment_is_recorded_with_the_series_it_belongs_to(self, practice):
        series = make_series(practice, occurrences=3)

        events = AuditEvent.objects.filter(verb=AuditVerb.BOOKING_CREATED)
        assert events.count() == 3
        assert {event.metadata["series"] for event in events} == {str(series.first.series_key)}


# --- the meeting link -----------------------------------------------------


class TestSettingAMeetingLink:
    def test_a_link_given_when_booking_is_on_every_week(self, practice):
        series = make_series(practice, occurrences=3, meeting_url="https://meet.example.org/ada")

        assert {b.meeting_url for b in series.bookings} == {"https://meet.example.org/ada"}
        assert all(b.is_virtual for b in series.bookings)

    def test_a_link_added_afterwards_can_cover_the_rest_of_the_series(self, practice):
        series = make_series(practice, occurrences=4)

        changed = services.set_meeting_link(
            series.first,
            actor=practice.counselor,
            meeting_url="https://meet.example.org/ada",
            apply_to_series=True,
        )

        assert changed == 4
        assert set(
            Booking.objects.filter(series_key=series.first.series_key).values_list(
                "meeting_url", flat=True
            )
        ) == {"https://meet.example.org/ada"}

    def test_one_session_can_be_given_its_own_link(self, practice):
        series = make_series(practice, occurrences=3)

        services.set_meeting_link(
            series.bookings[1],
            actor=practice.counselor,
            meeting_url="https://meet.example.org/just-this-one",
            apply_to_series=False,
        )

        assert [Booking.objects.get(pk=b.pk).meeting_url for b in series.bookings] == [
            "",
            "https://meet.example.org/just-this-one",
            "",
        ]

    def test_a_cancelled_week_is_left_out_of_a_series_update(self, practice):
        """A link to the room for something cancelled is an invitation to nothing."""
        series = make_series(practice, occurrences=3)
        services.cancel(series.bookings[1], actor=practice.counselor)

        changed = services.set_meeting_link(
            series.first,
            actor=practice.counselor,
            meeting_url="https://meet.example.org/ada",
            apply_to_series=True,
        )

        assert changed == 2
        assert Booking.objects.get(pk=series.bookings[1].pk).meeting_url == ""

    def test_clearing_it_makes_the_session_in_person_again(self, practice):
        series = make_series(practice, occurrences=2, meeting_url="https://meet.example.org/ada")

        services.set_meeting_link(
            series.first, actor=practice.counselor, meeting_url="", apply_to_series=True
        )

        assert not Booking.objects.get(pk=series.first.pk).is_virtual

    def test_an_http_link_is_refused(self, practice):
        """A session joined over a plain connection is one anybody on the path can join."""
        series = make_series(practice)

        with pytest.raises(services.SchedulingError):
            services.set_meeting_link(
                series.first, actor=practice.counselor, meeting_url="http://meet.example.org/ada"
            )

        assert Booking.objects.get(pk=series.first.pk).meeting_url == ""

    def test_something_that_is_not_a_link_is_refused(self, practice):
        series = make_series(practice)

        with pytest.raises(services.SchedulingError):
            services.set_meeting_link(
                series.first, actor=practice.counselor, meeting_url="javascript:alert(1)"
            )

    def test_the_trail_records_that_it_is_virtual_and_never_the_link(self, practice):
        series = make_series(practice, occurrences=2)

        services.set_meeting_link(
            series.first,
            actor=practice.counselor,
            meeting_url="https://meet.example.org/secret-room",
            apply_to_series=True,
        )

        events = AuditEvent.objects.filter(verb=AuditVerb.BOOKING_MEETING_LINK_SET)
        assert events.count() == 2
        for event in events:
            assert event.metadata["is_virtual"] is True
            assert "secret-room" not in str(event.metadata)


class TestWhatTheCounseleeSees:
    def test_the_appointment_page_offers_the_way_in(self, practice, client, sign_in):
        series = make_series(practice, occurrences=1, meeting_url="https://meet.example.org/ada")
        sign_in(practice.counselee)

        page = client.get(
            reverse("scheduling:detail", kwargs={"public_id": series.first.public_id})
        ).content.decode()

        assert "Join meeting" in page
        assert "https://meet.example.org/ada" in page
        # Without this the meeting provider is handed the appointment's own URL.
        assert 'rel="noopener noreferrer"' in page

    def test_a_cancelled_appointment_offers_no_way_in(self, practice, client, sign_in):
        series = make_series(practice, occurrences=1, meeting_url="https://meet.example.org/ada")
        services.cancel(series.first, actor=practice.counselor)
        sign_in(practice.counselee)

        page = client.get(
            reverse("scheduling:detail", kwargs={"public_id": series.first.public_id})
        ).content.decode()

        assert "Join meeting" not in page

    def test_an_in_person_appointment_says_so_and_offers_nothing_to_click(
        self, practice, client, sign_in
    ):
        series = make_series(practice, occurrences=1)
        sign_in(practice.counselee)

        page = client.get(
            reverse("scheduling:detail", kwargs={"public_id": series.first.public_id})
        ).content.decode()

        assert "In person" in page
        assert "Join meeting" not in page

    def test_a_counselee_cannot_set_one(self, practice, client, sign_in):
        """Whoever sets the link decides which room the session happens in."""
        series = make_series(practice, occurrences=1)
        sign_in(practice.counselee)

        response = client.post(
            reverse("scheduling:meeting_link", kwargs={"public_id": series.first.public_id}),
            {"meeting_url": "https://meet.example.org/mine"},
        )

        assert response.status_code == 403
        assert Booking.objects.get(pk=series.first.pk).meeting_url == ""

    def test_the_link_is_in_the_confirmation_email(self, practice):
        mail.outbox.clear()

        make_series(practice, occurrences=2, meeting_url="https://meet.example.org/ada")

        assert "https://meet.example.org/ada" in mail.outbox[0].body

    def test_the_link_is_in_the_reminder(self, practice):
        series = make_series(practice, occurrences=1, meeting_url="https://meet.example.org/ada")
        mail.outbox.clear()

        services.send_reminder(series.first)

        assert "https://meet.example.org/ada" in mail.outbox[0].body


# --- the page a counselor uses --------------------------------------------


class TestTheSchedulingPage:
    def test_it_offers_the_recurring_option(self, practice, client, sign_in):
        sign_in(practice.counselor)

        page = client.get(
            reverse("scheduling:schedule", kwargs={"case_public_id": practice.case.public_id})
        ).content.decode()

        assert "Set recurring" in page
        assert "repeat=weekly" in page

    def test_the_recurring_form_asks_for_a_weekday_and_a_count(self, practice, client, sign_in):
        sign_in(practice.counselor)

        page = client.get(
            reverse("scheduling:schedule", kwargs={"case_public_id": practice.case.public_id})
            + "?repeat=weekly"
        ).content.decode()

        assert "Day of the week" in page
        assert "How many sessions?" in page
        # The date field is gone: it is the thing the weekday replaces.
        assert 'name="date"' not in page

    def test_posting_it_books_the_series(self, practice, client, sign_in):
        sign_in(practice.counselor)

        response = client.post(
            reverse("scheduling:schedule", kwargs={"case_public_id": practice.case.public_id})
            + "?repeat=weekly",
            {
                "counselee": practice.counselee.pk,
                "weekday": Weekday.TUESDAY,
                "time": "14:00",
                "minutes": 60,
                "occurrences": 5,
                "attendance": Attendance.INDIVIDUAL,
                "meeting_url": "https://meet.example.org/ada",
            },
        )

        assert response.status_code == 302
        bookings = Booking.objects.filter(case=practice.case)
        assert bookings.count() == 5
        assert {b.meeting_url for b in bookings} == {"https://meet.example.org/ada"}

    def test_a_pasted_link_without_a_scheme_is_completed_rather_than_refused(
        self, practice, client, sign_in
    ):
        """What a person copies out of a chat message has no https:// on the front."""
        sign_in(practice.counselor)

        client.post(
            reverse("scheduling:schedule", kwargs={"case_public_id": practice.case.public_id}),
            {
                "counselee": practice.counselee.pk,
                "date": (timezone.localdate() + timedelta(days=3)).isoformat(),
                "time": "14:00",
                "minutes": 60,
                "attendance": Attendance.INDIVIDUAL,
                "meeting_url": "meet.example.org/ada",
            },
        )

        assert Booking.objects.get(case=practice.case).meeting_url == "https://meet.example.org/ada"

    def test_a_counselee_cannot_reach_the_recurring_form(self, practice, client, sign_in):
        sign_in(practice.counselee)

        response = client.get(
            reverse("scheduling:schedule", kwargs={"case_public_id": practice.case.public_id})
            + "?repeat=weekly"
        )

        assert response.status_code == 403


class TestTheRecurringForm:
    def test_the_first_session_is_the_next_of_that_weekday(self, practice):
        zone = practice.counselor.zoneinfo
        form = RecurringBookingForm(
            {
                "counselee": practice.counselee.pk,
                "weekday": Weekday.THURSDAY,
                "time": "14:00",
                "minutes": 60,
                "occurrences": 3,
                "attendance": Attendance.INDIVIDUAL,
                "meeting_url": "",
            },
            case=practice.case,
        )

        assert form.is_valid(), form.errors
        start = form.cleaned_data["start"].astimezone(zone)
        assert start.weekday() == Weekday.THURSDAY
        assert start.hour == 14
        assert start > timezone.now()

    def test_a_time_that_has_passed_today_means_next_week(self, practice):
        """Arranging Tuesdays at two on a Tuesday evening does not mean this afternoon."""
        zone = practice.counselor.zoneinfo
        today = timezone.now().astimezone(zone)
        form = RecurringBookingForm(
            {
                "counselee": practice.counselee.pk,
                "weekday": today.weekday(),
                "time": "00:00",
                "minutes": 60,
                "occurrences": 2,
                "attendance": Attendance.INDIVIDUAL,
                "meeting_url": "",
            },
            case=practice.case,
        )

        assert form.is_valid(), form.errors
        first = form.cleaned_data["start"].astimezone(zone)
        assert first.date() == today.date() + timedelta(days=7)

    def test_one_session_is_not_a_series(self, practice):
        form = RecurringBookingForm(
            {
                "counselee": practice.counselee.pk,
                "weekday": Weekday.TUESDAY,
                "time": "14:00",
                "minutes": 60,
                "occurrences": 1,
                "attendance": Attendance.INDIVIDUAL,
                "meeting_url": "",
            },
            case=practice.case,
        )

        assert not form.is_valid()
        assert "occurrences" in form.errors
