"""
The booking service — everything that changes the diary.

Three groups of promise here, in descending order of how badly a regression
would hurt:

  * **A slot cannot be taken twice.** Not by two counselees racing, and not by a
    counselor booking over something arranged elsewhere. That is the exclusion
    constraint's job rather than a check in application code, so the tests drive
    ``services.book`` and let Postgres do the refusing — including from two real
    connections at once, at the bottom of this file.
  * **A cancellation records the rule that was in force on the day.** The notice
    period is a mutable counselor setting, so a v3 invoice cannot re-derive
    "was this short notice" from timestamps months later.
  * **A reminder is sent once.** Two overlapping cron runs must not email a
    counselee twice at 6am.

tests/test_slots.py covers what is *offered*. This file covers what happens when
somebody tries to take it.
"""

import io
import threading
from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from django.core import mail
from django.db import OperationalError, connection
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember, CounselorProfile
from apps.scheduling import services
from apps.scheduling.models import (
    Attendance,
    AvailabilityRule,
    Booking,
    BookingStatus,
    Weekday,
)

pytestmark = pytest.mark.django_db


def deadlock():
    """An ``OperationalError`` shaped the way one really arrives from Postgres.

    Django wraps the driver's exception and leaves the original on ``__cause__``,
    which is the only place the SQLSTATE lives — so a bare
    ``OperationalError("deadlock detected")`` would not exercise the check at all.
    The real psycopg class is used rather than a stub carrying a ``sqlstate``
    attribute, because the point is that we read what the driver actually sets.
    """
    wrapped = OperationalError("deadlock detected")
    wrapped.__cause__ = psycopg.errors.DeadlockDetected("deadlock detected")
    return wrapped


def open_all_week(counselor, *, start="09:00", end="17:00", minutes=60):
    """Office hours on every weekday, so a test never has to find a Tuesday.

    Wide hours on purpose: the point of most tests here is what happens when two
    people want the same slot, and arranging for the slot to exist should not be
    the interesting part.
    """
    for weekday in Weekday.values:
        AvailabilityRule.objects.create(
            counselor=counselor,
            weekday=weekday,
            start_time=start,
            end_time=end,
            slot_minutes=minutes,
        )


@pytest.fixture
def practice(counselor, counselee):
    """One counselor with a full week of hours, one case, one counselee on it."""
    CounselorProfile.objects.create(user=counselor)
    open_all_week(counselor)
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    CaseMember.objects.create(case=case, counselee=counselee)
    return SimpleNamespace(case=case, counselor=counselor, counselee=counselee)


@pytest.fixture
def second_counselee(practice, make_user):
    """A spouse on the same case — the shape that makes a clash likely."""
    other = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=practice.case, counselee=other)
    return other


def offered(practice, counselee=None):
    return services.bookable_slots(
        counselor=practice.counselor,
        counselee=counselee or practice.counselee,
    )


def book_at(practice, slot, *, counselee=None, created_by=None, **kwargs):
    counselee = counselee or practice.counselee
    return services.book(
        case=practice.case,
        counselee=counselee,
        start=slot.start,
        minutes=slot.minutes,
        created_by=created_by or counselee,
        **kwargs,
    )


# --- creating -------------------------------------------------------------


class TestBooking:
    def test_a_counselee_booking_themselves_in_only_requests_it(self, practice):
        """The counselor still has to agree. That is what ``requested`` means."""
        booking = book_at(practice, offered(practice)[0])

        assert booking.status == BookingStatus.REQUESTED
        assert booking.counselor == practice.counselor
        assert booking.created_by == practice.counselee

    def test_a_counselor_booking_it_is_already_confirmed(self, practice):
        """Staff scheduling something has made the decision confirmation records."""
        booking = book_at(practice, offered(practice)[0], created_by=practice.counselor)

        assert booking.status == BookingStatus.CONFIRMED

    def test_an_admin_booking_it_is_already_confirmed(self, practice, admin_user):
        booking = book_at(practice, offered(practice)[0], created_by=admin_user)

        assert booking.status == BookingStatus.CONFIRMED

    def test_a_time_that_is_not_offered_is_refused(self, practice):
        """3am is inside no office hour, so no amount of posting makes it bookable."""
        three_am = offered(practice)[0].start.replace(hour=3)

        with pytest.raises(services.NotBookable):
            services.book(
                case=practice.case,
                counselee=practice.counselee,
                start=three_am,
                minutes=60,
                created_by=practice.counselee,
            )

    def test_a_counselor_may_book_outside_the_published_hours(self, practice):
        """An urgent Saturday session is real. The hours tell counselees what to ask
        for; they do not overrule the counselor."""
        three_am = offered(practice)[0].start.replace(hour=3)

        booking = services.book(
            case=practice.case,
            counselee=practice.counselee,
            start=three_am,
            minutes=60,
            created_by=practice.counselor,
            enforce_availability=False,
        )

        assert booking.status == BookingStatus.CONFIRMED

    def test_somebody_who_has_left_the_case_cannot_be_booked(self, practice, second_counselee):
        practice.case.members.filter(counselee=second_counselee).update(ended_on=org_today())

        with pytest.raises(services.NotBookable):
            book_at(practice, offered(practice)[0], counselee=second_counselee)

    def test_booking_emails_and_audits(self, practice):
        booking = book_at(practice, offered(practice)[0])

        assert len(mail.outbox) == 1
        # The counselor is the one told, because it is a request awaiting them.
        assert mail.outbox[0].to == [practice.counselor.email]
        assert AuditEvent.objects.filter(
            verb=AuditVerb.BOOKING_CREATED, target_id=str(booking.pk)
        ).exists()

    def test_a_joint_booking_tells_everyone_on_the_case(self, practice, second_counselee):
        book_at(
            practice,
            offered(practice)[0],
            created_by=practice.counselor,
            attendance=Attendance.WHOLE_CASE,
        )

        recipients = {address for message in mail.outbox for address in message.to}
        assert recipients == {practice.counselee.email, second_counselee.email}

    def test_the_request_note_stays_out_of_the_audit_trail(self, practice):
        """Counseling content. The trail is read by people not entitled to it."""
        booking = book_at(practice, offered(practice)[0], request_note="My marriage is failing.")

        event = AuditEvent.objects.get(verb=AuditVerb.BOOKING_CREATED, target_id=str(booking.pk))
        assert "failing" not in str(event.metadata)


# --- clashes --------------------------------------------------------------


class TestClashes:
    def test_a_counselee_is_told_a_taken_time_is_not_on_offer(self, practice, second_counselee):
        """The courteous path. A taken slot has already left the generated list, so
        the availability check refuses before the constraint is ever reached."""
        slot = offered(practice)[0]
        book_at(practice, slot)

        with pytest.raises(services.NotBookable):
            book_at(practice, slot, counselee=second_counselee)

        assert Booking.objects.count() == 1

    def test_a_counselor_booking_over_a_taken_slot_hits_the_constraint(
        self, practice, second_counselee
    ):
        """The reliable path, and the one a real race takes.

        A counselor is not held to the published hours, so nothing checks first —
        the insert goes in and Postgres refuses it.
        """
        slot = offered(practice)[0]
        book_at(practice, slot)

        with pytest.raises(services.SlotUnavailable) as refusal:
            book_at(
                practice,
                slot,
                counselee=second_counselee,
                created_by=practice.counselor,
                enforce_availability=False,
            )

        assert Booking.objects.count() == 1
        # A sentence, not a schema detail. Django's own constraint validation would
        # have quoted the constraint name at whoever was booking, which is why
        # ``_validate`` passes ``validate_constraints=False``.
        assert "constraint" not in str(refusal.value).lower()
        assert "no_overlapping" not in str(refusal.value)

    def test_back_to_back_appointments_are_both_allowed(self, practice, second_counselee):
        """Half-open ranges. With inclusive bounds a full morning would be impossible."""
        first, second = offered(practice)[:2]
        assert first.end == second.start

        book_at(practice, first)
        book_at(practice, second, counselee=second_counselee)

        assert Booking.objects.count() == 2

    def test_a_counselee_cannot_be_in_two_places_at_once(self, practice, make_user):
        """The second constraint, and a different mistake from the first.

        Usually a counselor booking over something the counselee arranged with
        another counselor, which the per-counselor constraint would not catch.
        """
        elsewhere = make_user(Role.COUNSELOR)
        open_all_week(elsewhere)
        other_case = Case.objects.create(counselor=elsewhere, label="Ashford — individual")
        CaseMember.objects.create(case=other_case, counselee=practice.counselee)

        slot = offered(practice)[0]
        book_at(practice, slot)

        with pytest.raises(services.SlotUnavailable) as refusal:
            services.book(
                case=other_case,
                counselee=practice.counselee,
                start=slot.start,
                minutes=slot.minutes,
                created_by=elsewhere,
                enforce_availability=False,
            )

        # And the message names nobody. That the person is busy is the minimum
        # needed to explain the refusal; who else they see is not this counselor's
        # business, and neither is which case.
        assert "already have an appointment" in str(refusal.value)
        assert practice.counselor.full_name not in str(refusal.value)

    def test_a_taken_slot_is_no_longer_offered(self, practice, second_counselee):
        """The courteous half of the defence, so the constraint is a rarity.

        Read unscoped on purpose — the spouse's appointment has to disappear from
        the list even though its row is invisible to them.
        """
        slot = offered(practice)[0]
        book_at(practice, slot)

        assert slot not in offered(practice, counselee=second_counselee)

    def test_cancelling_puts_the_time_back_on_offer(self, practice):
        slot = offered(practice)[0]
        booking = book_at(practice, slot)
        services.cancel(booking, actor=practice.counselee)

        assert slot in offered(practice)
        # And the constraint no longer objects, because it only covers live rows.
        assert book_at(practice, slot).pk != booking.pk


# --- cancelling -----------------------------------------------------------


class TestCancelling:
    def make(self, practice, *, hours_ahead, status=BookingStatus.CONFIRMED):
        start = timezone.now() + timedelta(hours=hours_ahead)
        return Booking.objects.create(
            counselor=practice.counselor,
            case=practice.case,
            counselee=practice.counselee,
            slot=Booking.range_for(start, 60),
            status=status,
            created_by=practice.counselee,
        )

    def test_cancelling_inside_the_notice_period_is_recorded_as_such(self, practice):
        """Recorded now, not derived later: the notice period is a mutable setting
        and next month's invoice must reflect the rule in force on the day."""
        booking = self.make(practice, hours_ahead=2)

        services.cancel(booking, actor=practice.counselee)

        booking.refresh_from_db()
        assert booking.status == BookingStatus.CANCELLED
        assert booking.was_late_cancellation is True
        assert booking.cancelled_by == practice.counselee
        assert booking.cancelled_at is not None

    def test_cancelling_with_notice_is_not(self, practice):
        booking = self.make(practice, hours_ahead=72)

        services.cancel(booking, actor=practice.counselee)

        booking.refresh_from_db()
        assert booking.was_late_cancellation is False

    def test_the_notice_period_comes_from_the_counselors_own_settings(self, practice):
        CounselorProfile.objects.filter(user=practice.counselor).update(booking_notice_hours=96)
        booking = self.make(practice, hours_ahead=72)

        services.cancel(booking, actor=practice.counselee)

        booking.refresh_from_db()
        assert booking.was_late_cancellation is True

    def test_the_reason_is_kept_on_the_booking_but_not_in_the_trail(self, practice):
        booking = self.make(practice, hours_ahead=72)

        services.cancel(booking, actor=practice.counselee, reason="My father is in hospital.")

        booking.refresh_from_db()
        assert booking.cancellation_reason == "My father is in hospital."
        event = AuditEvent.objects.get(verb=AuditVerb.BOOKING_CANCELLED)
        assert "hospital" not in str(event.metadata)
        assert event.metadata["reason_given"] is True

    def test_cancelling_notifies_the_other_side_and_not_the_canceller(self, practice):
        booking = self.make(practice, hours_ahead=72)

        services.cancel(booking, actor=practice.counselee)

        recipients = {address for message in mail.outbox for address in message.to}
        assert recipients == {practice.counselor.email}

    def test_an_already_cancelled_appointment_cannot_be_cancelled_again(self, practice):
        booking = self.make(practice, hours_ahead=72)
        services.cancel(booking, actor=practice.counselee)

        with pytest.raises(services.SchedulingError):
            services.cancel(booking, actor=practice.counselee)


# --- confirming, moving, closing out --------------------------------------


class TestLifecycle:
    def make(self, practice, *, hours_ahead, status=BookingStatus.REQUESTED, **kwargs):
        start = timezone.now() + timedelta(hours=hours_ahead)
        return Booking.objects.create(
            counselor=practice.counselor,
            case=practice.case,
            counselee=practice.counselee,
            slot=Booking.range_for(start, 60),
            status=status,
            created_by=practice.counselee,
            **kwargs,
        )

    def test_confirming_a_request_accepts_it(self, practice):
        booking = self.make(practice, hours_ahead=72)

        services.confirm(booking, actor=practice.counselor)

        booking.refresh_from_db()
        assert booking.status == BookingStatus.CONFIRMED
        assert mail.outbox[-1].to == [practice.counselee.email]

    def test_confirming_twice_is_a_double_clicked_button_not_an_error(self, practice):
        booking = self.make(practice, hours_ahead=72)
        services.confirm(booking, actor=practice.counselor)
        sent = len(mail.outbox)

        services.confirm(booking, actor=practice.counselor)

        assert len(mail.outbox) == sent

    def test_a_cancelled_appointment_cannot_be_confirmed(self, practice):
        booking = self.make(
            practice, hours_ahead=72, status=BookingStatus.CANCELLED, cancelled_at=timezone.now()
        )

        with pytest.raises(services.SchedulingError):
            services.confirm(booking, actor=practice.counselor)

    def test_rescheduling_keeps_one_row(self, practice):
        """One row rather than cancel-and-rebook, so the note and the history stay
        attached to the appointment that actually happens."""
        booking = self.make(practice, hours_ahead=72, request_note="Please keep this.")
        moved_to = booking.starts_at + timedelta(days=1)

        services.reschedule(booking, actor=practice.counselor, start=moved_to)

        assert Booking.objects.count() == 1
        booking.refresh_from_db()
        assert booking.starts_at == moved_to
        assert booking.request_note == "Please keep this."

    def test_rescheduling_makes_the_appointment_eligible_for_a_fresh_reminder(self, practice):
        """The reminder already sent describes a time that no longer applies."""
        booking = self.make(practice, hours_ahead=72, reminder_sent_at=timezone.now())

        services.reschedule(
            booking, actor=practice.counselor, start=booking.starts_at + timedelta(days=1)
        )

        booking.refresh_from_db()
        assert booking.reminder_sent_at is None

    def test_rescheduling_onto_a_taken_time_is_refused(self, practice, second_counselee):
        booking = self.make(practice, hours_ahead=72)
        occupied = Booking.objects.create(
            counselor=practice.counselor,
            case=practice.case,
            counselee=second_counselee,
            slot=Booking.range_for(booking.starts_at + timedelta(days=1), 60),
            status=BookingStatus.CONFIRMED,
            created_by=practice.counselor,
        )

        with pytest.raises(services.SlotUnavailable):
            services.reschedule(booking, actor=practice.counselor, start=occupied.starts_at)

        booking.refresh_from_db()
        assert booking.starts_at != occupied.starts_at

    def test_an_appointment_that_has_not_happened_cannot_be_closed_out(self, practice):
        """An outcome is a record of the past, and in v3 it is what raises an invoice."""
        booking = self.make(practice, hours_ahead=72, status=BookingStatus.CONFIRMED)

        with pytest.raises(services.SchedulingError):
            services.mark_completed(booking, actor=practice.counselor)

    def test_a_past_session_can_be_recorded_as_held(self, practice):
        booking = self.make(practice, hours_ahead=-72, status=BookingStatus.CONFIRMED)

        services.mark_completed(
            booking, actor=practice.counselor, note="Worked through Ephesians 4."
        )

        booking.refresh_from_db()
        assert booking.status == BookingStatus.COMPLETED
        assert booking.counselor_note == "Worked through Ephesians 4."

    def test_a_past_session_can_be_recorded_as_missed(self, practice):
        """Kept distinct from a cancellation and from a held session: three
        different things to bill for."""
        booking = self.make(practice, hours_ahead=-72, status=BookingStatus.CONFIRMED)

        services.mark_no_show(booking, actor=practice.counselor)

        booking.refresh_from_db()
        assert booking.status == BookingStatus.NO_SHOW

    def test_an_outcome_can_be_corrected(self, practice):
        booking = self.make(practice, hours_ahead=-72, status=BookingStatus.CONFIRMED)
        services.mark_no_show(booking, actor=practice.counselor)

        services.mark_completed(booking, actor=practice.counselor)

        booking.refresh_from_db()
        assert booking.status == BookingStatus.COMPLETED

    def test_a_cancelled_session_cannot_be_recorded_as_held(self, practice):
        booking = self.make(
            practice,
            hours_ahead=-72,
            status=BookingStatus.CANCELLED,
            cancelled_at=timezone.now(),
        )

        with pytest.raises(services.SchedulingError):
            services.mark_completed(booking, actor=practice.counselor)

    def test_the_session_note_stays_out_of_the_audit_trail(self, practice):
        booking = self.make(practice, hours_ahead=-72, status=BookingStatus.CONFIRMED)

        services.mark_completed(booking, actor=practice.counselor, note="Disclosed self-harm.")

        event = AuditEvent.objects.get(verb=AuditVerb.BOOKING_COMPLETED)
        assert "self-harm" not in str(event.metadata)
        assert event.metadata["note_recorded"] is True


# --- reminders ------------------------------------------------------------


class TestReminders:
    def make(self, practice, *, hours_ahead, status=BookingStatus.CONFIRMED, **kwargs):
        start = timezone.now() + timedelta(hours=hours_ahead)
        return Booking.objects.create(
            counselor=practice.counselor,
            case=practice.case,
            counselee=practice.counselee,
            slot=Booking.range_for(start, 60),
            status=status,
            created_by=practice.counselee,
            **kwargs,
        )

    def test_a_confirmed_appointment_starting_soon_is_due(self, practice):
        booking = self.make(practice, hours_ahead=6)

        assert list(services.due_reminders()) == [booking]

    def test_an_unconfirmed_request_is_not(self, practice):
        """Reminding somebody about a time the counselor has not agreed to would be
        worse than saying nothing."""
        self.make(practice, hours_ahead=6, status=BookingStatus.REQUESTED)

        assert list(services.due_reminders()) == []

    def test_an_appointment_beyond_the_window_is_not(self, practice):
        self.make(practice, hours_ahead=48)

        assert list(services.due_reminders()) == []

    def test_one_already_reminded_about_is_not(self, practice):
        self.make(practice, hours_ahead=6, reminder_sent_at=timezone.now())

        assert list(services.due_reminders()) == []

    def test_a_reminder_is_sent_once_however_often_the_job_runs(self, practice):
        """Claimed with a conditional UPDATE before the mail goes out, so two
        overlapping cron runs cannot both email a counselee at 6am."""
        booking = self.make(practice, hours_ahead=6)

        assert services.send_reminder(booking) is True
        assert services.send_reminder(booking) is False
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == [practice.counselee.email]

    def test_a_joint_appointment_reminds_everyone_expected(self, practice, second_counselee):
        booking = self.make(practice, hours_ahead=6, attendance=Attendance.WHOLE_CASE)

        services.send_reminder(booking)

        recipients = {address for message in mail.outbox for address in message.to}
        assert recipients == {practice.counselee.email, second_counselee.email}


class TestTheReminderCommand:
    """The cron sidecar's entry point — see compose/cron/bctracker.cron.

    Worth testing rather than trusting, because nothing else in the system notices
    when a scheduled job silently stops working: Workspace SMTP has no bounce
    reporting, so an unsent reminder looks exactly like a sent one.
    """

    def make(self, practice, *, hours_ahead, **kwargs):
        start = timezone.now() + timedelta(hours=hours_ahead)
        return Booking.objects.create(
            counselor=practice.counselor,
            case=practice.case,
            counselee=practice.counselee,
            slot=Booking.range_for(start, 60),
            status=BookingStatus.CONFIRMED,
            created_by=practice.counselee,
            **kwargs,
        )

    def run(self, **options):
        from django.core.management import call_command

        output = io.StringIO()
        call_command("send_appointment_reminders", stdout=output, **options)
        return output.getvalue()

    def test_it_sends_what_is_due(self, practice):
        booking = self.make(practice, hours_ahead=6)

        output = self.run()

        booking.refresh_from_db()
        assert booking.reminder_sent_at is not None
        assert len(mail.outbox) == 1
        assert "1 reminder(s) sent" in output

    def test_running_it_twice_does_not_remind_twice(self, practice):
        """Hourly with a 24-hour window is the intended configuration, so most runs
        see appointments a previous run already handled."""
        self.make(practice, hours_ahead=6)

        self.run()
        self.run()

        assert len(mail.outbox) == 1

    def test_the_window_is_configurable(self, practice):
        self.make(practice, hours_ahead=40)

        assert "0 reminder(s) sent" in self.run()
        assert "1 reminder(s) sent" in self.run(hours=48)

    def test_a_dry_run_sends_and_marks_nothing(self, practice):
        booking = self.make(practice, hours_ahead=6)

        output = self.run(dry_run=True)

        booking.refresh_from_db()
        assert booking.reminder_sent_at is None
        assert mail.outbox == []
        assert str(booking.pk) in output


# --- the booking window ---------------------------------------------------


class TestBookingWindow:
    def test_a_counselor_with_no_profile_still_has_workable_defaults(self, counselor):
        """Somebody an administrator created a minute ago must not break the page."""
        window = services.BookingWindow(counselor)

        assert window.session_minutes == services.FALLBACK_SESSION_MINUTES
        assert window.horizon_days == services.FALLBACK_HORIZON_DAYS

    def test_the_requested_range_is_clamped_to_the_horizon(self, practice):
        """Clamped rather than validated: a date in 2031 should show an empty week
        rather than an error page."""
        window = services.BookingWindow(practice.counselor)
        now = timezone.now()
        today = now.astimezone(window.zone).date()

        start, end = window.dates(
            now=now,
            start_date=today - timedelta(days=30),
            end_date=today + timedelta(days=3650),
        )

        assert start == today
        assert end == today + timedelta(days=window.horizon_days)

    def test_nothing_is_offered_beyond_the_horizon(self, practice):
        window = services.BookingWindow(practice.counselor)
        far = timezone.localdate() + timedelta(days=window.horizon_days + 30)

        assert (
            services.bookable_slots(counselor=practice.counselor, start_date=far, end_date=far)
            == []
        )

    def test_nothing_is_offered_inside_the_notice_period(self, practice):
        """The default is 24 hours, so nothing today can be booked by a counselee."""
        today = timezone.localdate()

        assert (
            services.bookable_slots(counselor=practice.counselor, start_date=today, end_date=today)
            == []
        )


class TestLosingTheRace:
    """Both ways Postgres can refuse a simultaneous booking, pinned deterministically.

    The threaded test at the bottom of this file proves the guarantee but cannot
    choose which shape it gets. These can, by handing ``book`` a connection that
    raises what Postgres would have.
    """

    @pytest.fixture
    def bookable(self, counselor, counselee):
        CounselorProfile.objects.create(user=counselor)
        case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
        CaseMember.objects.create(case=case, counselee=counselee)
        return SimpleNamespace(
            case=case,
            counselee=counselee,
            counselor=counselor,
            start=(timezone.now() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0),
        )

    def _raising(self, exc):
        def save(*args, **kwargs):
            raise exc

        return save

    def test_a_deadlock_is_the_same_refusal_as_a_clash(self, monkeypatch, bookable):
        """40P01, which is what the loser actually gets when the inserts are
        simultaneous rather than merely close together."""
        monkeypatch.setattr(Booking, "save", self._raising(deadlock()), raising=True)

        with pytest.raises(services.SlotUnavailable):
            services.book(
                case=bookable.case,
                counselee=bookable.counselee,
                start=bookable.start,
                minutes=60,
                created_by=bookable.counselor,
                enforce_availability=False,
            )

    def test_a_reschedule_that_deadlocks_keeps_its_old_time(self, monkeypatch, bookable):
        booking = services.book(
            case=bookable.case,
            counselee=bookable.counselee,
            start=bookable.start,
            minutes=60,
            created_by=bookable.counselor,
            enforce_availability=False,
        )
        monkeypatch.setattr(Booking, "save", self._raising(deadlock()), raising=True)

        with pytest.raises(services.SlotUnavailable):
            services.reschedule(
                booking,
                actor=bookable.counselor,
                start=bookable.start + timedelta(hours=2),
            )

        # Refreshed rather than left carrying a time it does not have, so a view
        # rendering the booking after the refusal shows the truth.
        assert booking.starts_at == bookable.start

    def test_a_database_that_is_simply_down_is_not_reported_as_a_busy_diary(
        self, monkeypatch, bookable
    ):
        """The reason ``_is_booking_race`` looks at the SQLSTATE.

        "That time has just been taken" for a dropped connection would send
        somebody round the booking page for ever.
        """
        monkeypatch.setattr(
            Booking,
            "save",
            self._raising(OperationalError("server closed the connection unexpectedly")),
            raising=True,
        )

        with pytest.raises(OperationalError):
            services.book(
                case=bookable.case,
                counselee=bookable.counselee,
                start=bookable.start,
                minutes=60,
                created_by=bookable.counselor,
                enforce_availability=False,
            )


# --- two connections, one slot -------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_two_connections_booking_one_slot_and_exactly_one_wins(
    counselor, counselee, make_user, settings
):
    """The claim the exclusion constraint exists to make, proved the hard way.

    ``transaction=True`` so the rows are really committed and a second connection
    can see them; two threads, each with its own connection, released together by
    a barrier. An application-level "is it free?" check would let both through
    here, and the failure would only ever show up in production as two people in
    the waiting room.

    The loser loses in one of two ways depending on how closely the two inserts
    land — an exclusion violation, or a deadlock when each is already waiting on
    the other's uncommitted row — and which one happens is not deterministic. Both
    have to arrive as ``SlotUnavailable``, so this asserts the refusal rather than
    the mechanism; ``TestLosingTheRace`` pins each shape on its own.
    """
    CounselorProfile.objects.create(user=counselor)
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    CaseMember.objects.create(case=case, counselee=counselee)
    spouse = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=spouse)

    start = (timezone.now() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    ready = threading.Barrier(2)
    outcomes = []

    def attempt(who):
        try:
            ready.wait(timeout=10)
            services.book(
                case=case,
                counselee=who,
                start=start,
                minutes=60,
                created_by=counselor,
                # Not the point of this test, and it would need the slot to fall
                # inside the generated hours on whatever day the suite runs.
                enforce_availability=False,
            )
            outcomes.append("booked")
        except services.SlotUnavailable:
            outcomes.append("refused")
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            # Named in the outcome rather than left to surface as an unhandled
            # thread warning, so a regression says what went wrong.
            outcomes.append(f"{type(exc).__name__}: {exc}")
        finally:
            # Each thread gets its own connection; leaving it open would strand a
            # Postgres backend and the test database could not be torn down.
            connection.close()

    threads = [threading.Thread(target=attempt, args=(who,)) for who in (counselee, spouse)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == ["booked", "refused"]
    assert (
        Booking.objects.filter(
            status__in=(BookingStatus.REQUESTED, BookingStatus.CONFIRMED)
        ).count()
        == 1
    )
