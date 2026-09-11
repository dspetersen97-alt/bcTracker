"""
Booking, confirming, cancelling — everything that changes the diary.

Kept out of the views so the awkward rules are testable without HTTP and so the
reminder command can act without a request object. Three things here are worth
reading before changing anything:

**Double booking is settled by Postgres, not by this module.** ``book`` does not
check for a clash before inserting. It inserts, and catches the
``IntegrityError`` the exclusion constraint raises. Checking first would be a
read-then-write race that two counselees clicking the same 10am would win
together; the constraint cannot be raced, and it holds against code written later
that forgets to look. Losing that race comes back in *two* shapes, and both mean
the same thing to the person clicking — see ``_is_booking_race``.

**Availability is computed unscoped, and returns only times.** ``bookable_slots``
reads every active booking for the counselor regardless of whose it is, because a
slot occupied by another counselee's appointment has to disappear. It returns
``Slot`` objects and nothing else — no names, no case ids — so a counselee learns
that Tuesday 10am is taken and never who has it.

**Email failure never loses an appointment.** Notifications go through
``notify``, which logs and swallows. Workspace SMTP has a daily cap and no bounce
reporting, so a send that fails must not roll back a booking the counselee has
already been told about.
"""

import logging
from datetime import datetime, time, timedelta
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.backends.postgresql.psycopg_any import DateTimeTZRange
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.counseling.models import CounselorProfile
from apps.scheduling import notify, slots
from apps.scheduling.models import (
    ACTIVE_STATUSES,
    MAX_SERIES_SESSIONS,
    Attendance,
    AvailabilityOverride,
    AvailabilityRule,
    Booking,
    BookingStatus,
)

logger = logging.getLogger(__name__)

#: Used when a counselor has no profile row yet, so office hours still work for
#: someone an administrator created a minute ago.
FALLBACK_SESSION_MINUTES = 60
FALLBACK_HORIZON_DAYS = 60


class SchedulingError(Exception):
    """Base for refusals whose message is safe to show the person acting."""


class SlotUnavailable(SchedulingError):
    """The time was taken between the page being rendered and the form posted."""

    def __init__(self, message=None):
        super().__init__(message or _("That time has just been taken. Please pick another."))


class NotBookable(SchedulingError):
    """The requested time is not one this counselor offers."""


#: The exclusion constraints, by name, so an IntegrityError can be turned into a
#: sentence instead of a schema detail. Matched against the driver's message,
#: which is the only place psycopg puts the constraint name.
CLASH_CONSTRAINTS = {
    "no_overlapping_bookings_per_counselor": None,  # the default SlotUnavailable text
    "no_overlapping_bookings_per_counselee": _("They already have an appointment at that time."),
}


def _refusal_for(exc: IntegrityError) -> SchedulingError:
    """Turn a constraint violation into something safe to show the person acting.

    Only the two clash constraints get a friendly message. Anything else is a bug
    rather than a busy diary, and claiming "that time has just been taken" about a
    broken foreign key would send somebody round the booking page for ever.

    The counselee-clash message names no counselor on purpose. That the person is
    busy is the minimum needed to explain the refusal; who else they see is not
    this counselor's business.
    """
    detail = str(exc)
    for name, message in CLASH_CONSTRAINTS.items():
        if name in detail:
            return SlotUnavailable(message)

    logger.error("Booking refused by an unexpected constraint: %s", exc)
    return SchedulingError(_("That appointment could not be saved."))


#: Postgres SQLSTATEs that mean "somebody else was writing the same interval at
#: the same instant". 40P01 is a deadlock, 40001 a serialization failure.
RACE_SQLSTATES = frozenset({"40P01", "40001"})


def _is_booking_race(exc: OperationalError) -> bool:
    """Was this the exclusion constraint losing a coin toss rather than an outage?

    Two transactions inserting overlapping appointments at genuinely the same
    moment do not queue politely. Each inserts its own row, then scans the GiST
    index for a clash, finds the other's uncommitted row, and waits for it — so
    each is waiting on the other and Postgres kills one with a **deadlock**
    (40P01) rather than an exclusion violation. Same cause as an ``IntegrityError``
    from the constraint, same thing to say to the person clicking, so it earns the
    same refusal instead of a 500.

    Narrowed to the SQLSTATE, because ``OperationalError`` is also what a dropped
    connection and a full disk arrive as, and telling somebody the slot was taken
    when the database is down would send them round the booking page for ever.
    """
    return getattr(exc.__cause__, "sqlstate", None) in RACE_SQLSTATES


# --- reading availability -------------------------------------------------


class BookingWindow:
    """A counselor's booking settings, resolved once.

    A small object rather than four loose variables because the booking form, the
    slot list, and the cancellation rule all need the same numbers and reading
    them twice invites them to disagree.
    """

    def __init__(self, counselor):
        profile = CounselorProfile.objects.filter(user=counselor).first()
        self.counselor = counselor
        self.zone = counselor.zoneinfo
        self.session_minutes = (
            profile.default_session_minutes if profile else FALLBACK_SESSION_MINUTES
        )
        self.notice_hours = profile.booking_notice_hours if profile else slots.DEFAULT_NOTICE_HOURS
        self.horizon_days = profile.booking_horizon_days if profile else FALLBACK_HORIZON_DAYS

    def dates(self, *, now, start_date=None, end_date=None):
        """The date range to generate over, clamped to today..horizon.

        Clamped rather than validated: the range comes from query parameters, and
        a counselee typing a date in 2031 should see an empty week rather than an
        error, while a date in the past must not be allowed to surface a slot
        that the notice rule would then reject anyway.
        """
        today = now.astimezone(self.zone).date()
        last = today + timedelta(days=self.horizon_days)
        start = max(start_date or today, today)
        end = min(end_date or last, last)
        return start, end


def bookable_slots(
    *,
    counselor,
    counselee=None,
    now=None,
    start_date=None,
    end_date=None,
    extra_busy=(),
    use_google=True,
):
    """Times a counselee could book with this counselor.

    ``counselee`` is optional and narrows nothing about the counselor's diary; it
    adds *that person's* other appointments to the busy list, so a slot they
    could not actually take — because they are already booked with someone else at
    that hour — is not offered and then refused by the constraint.

    ``extra_busy`` is a plain sequence of ``(start, end)`` pairs, so a caller can
    add busy time from anywhere without this function knowing where from.

    ``use_google`` reads the counselor's Google free/busy and adds it to that list,
    which is what stops a counselee being offered the hour of somebody's dentist
    appointment. Set False by ``is_bookable``, and the reason is worth stating: a
    counselor's own calendar must not be able to *retract* a slot between a
    counselee seeing it and posting the form. Google being briefly unreachable, or
    an all-day marker appearing, would otherwise turn a legitimate booking into
    "that is not a time I offer" with nothing the counselee could do about it. The
    check that cannot be raced is the exclusion constraint, and it does not need
    Google's help.
    """
    now = now or timezone.now()
    window = BookingWindow(counselor)
    start_date, end_date = window.dates(now=now, start_date=start_date, end_date=end_date)
    if start_date > end_date:
        return []

    if use_google:
        extra_busy = [*extra_busy, *_google_busy(counselor, window, start_date, end_date)]

    return slots.generate(
        rules=AvailabilityRule.objects.filter(counselor=counselor),
        overrides=AvailabilityOverride.objects.filter(
            counselor=counselor, date__gte=start_date, date__lte=end_date
        ),
        zone=window.zone,
        start_date=start_date,
        end_date=end_date,
        now=now,
        busy=[
            *_busy_intervals(
                counselor=counselor,
                counselee=counselee,
                zone=window.zone,
                start_date=start_date,
                end_date=end_date,
            ),
            *extra_busy,
        ],
        notice_hours=window.notice_hours,
        default_minutes=window.session_minutes,
    )


def _google_busy(counselor, window, start_date, end_date):
    """The counselor's other Google commitments over the window, or nothing.

    Never raises: ``sync.busy_from_google`` returns ``[]`` for every failure, and
    the import is local so a counselor with no connection costs nothing and the
    booking page has no dependency on the integration being importable.
    """
    from apps.scheduling.google import sync

    span_start = datetime.combine(start_date, time.min).replace(tzinfo=window.zone)
    span_end = datetime.combine(end_date + timedelta(days=1), time.min).replace(tzinfo=window.zone)
    return sync.busy_from_google(counselor, start=span_start, end=span_end)


def _busy_intervals(*, counselor, counselee, zone, start_date, end_date):
    """Occupied intervals in the window, as bare ``(start, end)`` pairs.

    Read through ``Booking.objects`` rather than ``for_actor``, which is the one
    place in the application that does so on purpose. Scoping here would offer a
    counselee a slot their spouse already holds, and the constraint would then
    refuse the booking with no explanation. Only the interval leaves this
    function — never the row — so nothing about the other appointment is
    disclosed.
    """
    span = DateTimeTZRange(
        datetime.combine(start_date, time.min).replace(tzinfo=zone),
        datetime.combine(end_date + timedelta(days=1), time.min).replace(tzinfo=zone),
        bounds="[)",
    )
    who = Q(counselor=counselor)
    if counselee is not None:
        who |= Q(counselee=counselee)

    occupied = Booking.objects.filter(who, status__in=ACTIVE_STATUSES, slot__overlap=span)
    return [(booking.slot.lower, booking.slot.upper) for booking in occupied.only("slot")]


def is_bookable(*, counselor, counselee, start, minutes, now=None):
    """Whether ``start`` is a slot this counselor is actually offering.

    Compares against the generated list rather than re-deriving the rules, so
    there is exactly one definition of what is on offer and a booking cannot slip
    through a check that the page it came from did not make.

    ``use_google=False``: see ``bookable_slots``. This check happens between a
    counselee choosing a time and the row being written, and a third party must not
    get a veto in that gap.
    """
    now = now or timezone.now()
    offered = bookable_slots(
        counselor=counselor,
        counselee=counselee,
        now=now,
        use_google=False,
        start_date=start.astimezone(counselor.zoneinfo).date(),
        end_date=start.astimezone(counselor.zoneinfo).date(),
    )
    return any(slot.start == start and slot.minutes == minutes for slot in offered)


def _push_to_google(booking) -> None:
    """Mirror one booking into the counselor's Google calendar, if they have one.

    Imported here rather than at module scope, and wrapped: the integration is
    optional and must never be able to undo an appointment. By the time this runs
    the booking is committed and the counselee has been emailed, so an exception
    escaping would report a failure for something that already happened. Anything
    missed is picked up by ``manage.py sync_google_calendar``.

    ``Exception`` rather than ``GoogleError`` on purpose. The sync layer already
    converts every expected failure; this catches the unexpected one — a bug in the
    event body, a library raising something new — because the alternative is a
    counseling appointment lost to a typo in an integration nobody needs.
    """
    from apps.scheduling.google import sync

    try:
        sync.push_booking(booking)
    except Exception:
        logger.exception("Pushing booking %s to Google failed unexpectedly.", booking.pk)


# --- writing --------------------------------------------------------------


def book(
    *,
    case,
    counselee,
    start,
    minutes=None,
    attendance=Attendance.INDIVIDUAL,
    request_note="",
    meeting_url="",
    created_by,
    enforce_availability=True,
    request=None,
):
    """Create one appointment. Returns the Booking.

    ``enforce_availability`` is True for a counselee booking themselves in and
    False when a counselor books on someone's behalf: a counselor fitting an
    urgent session in outside their published hours is a legitimate thing to do,
    and the office hours exist to tell counselees what to ask for, not to overrule
    the counselor.

    Raises ``SlotUnavailable`` if the time is taken and ``NotBookable`` if it was
    never on offer.
    """
    booking = _insert_booking(
        case=case,
        counselee=counselee,
        start=start,
        minutes=minutes,
        attendance=attendance,
        request_note=request_note,
        meeting_url=meeting_url,
        created_by=created_by,
        enforce_availability=enforce_availability,
        request=request,
    )
    notify.booking_created(booking)
    _push_to_google(booking)
    return booking


def _insert_booking(
    *,
    case,
    counselee,
    start,
    minutes=None,
    attendance=Attendance.INDIVIDUAL,
    request_note="",
    meeting_url="",
    series_key=None,
    created_by,
    enforce_availability=True,
    request=None,
):
    """Write one appointment and record it. No email, no calendar push.

    Split from ``book`` for the sake of ``book_series``, which needs ten rows and
    *one* email. Sending one confirmation per appointment would put ten near
    identical messages in a counselee's inbox for a single decision, and Workspace
    SMTP has a daily cap that a term of counseling for a handful of cases would
    reach on its own.

    The audit row is written here rather than by the caller, so a series cannot end
    up with appointments that nothing in the trail says were created.
    """
    counselor = case.counselor
    minutes = minutes or BookingWindow(counselor).session_minutes

    if enforce_availability and not is_bookable(
        counselor=counselor, counselee=counselee, start=start, minutes=minutes
    ):
        raise NotBookable(_("That time is not available. Please choose one of the times offered."))

    booking = Booking(
        counselor=counselor,
        case=case,
        counselee=counselee,
        attendance=attendance,
        slot=Booking.range_for(start, minutes),
        # A counselee asks; a counselor books. Staff scheduling something has
        # already made the decision the confirmation step exists to record.
        status=(
            BookingStatus.CONFIRMED if created_by.is_ministry_staff else BookingStatus.REQUESTED
        ),
        request_note=request_note,
        meeting_url=meeting_url,
        series_key=series_key,
        created_by=created_by,
    )
    _validate(booking)

    try:
        # Its own atomic block: an IntegrityError poisons the transaction, so the
        # rollback has to happen before anything else touches the connection.
        with transaction.atomic():
            booking.save()
    except IntegrityError as exc:
        logger.info("Booking refused by a constraint: %s", exc)
        raise _refusal_for(exc) from exc
    except OperationalError as exc:
        if not _is_booking_race(exc):
            raise
        logger.info("Booking lost a deadlock against a simultaneous one: %s", exc)
        raise SlotUnavailable() from exc

    record(
        AuditVerb.BOOKING_CREATED,
        actor=created_by,
        target=booking,
        request=request,
        case_id=case.pk,
        counselee_id=str(counselee.pk),
        counselor_id=str(counselor.pk),
        starts_at=booking.starts_at.isoformat(),
        minutes=minutes,
        attendance=attendance,
        status=booking.status,
        # Whether the session is virtual, never the link itself. The trail is read
        # by people who are not expected at the appointment, and a meeting link is a
        # way into the room.
        is_virtual=bool(meeting_url),
        series=str(series_key) if series_key else None,
    )
    return booking


class Series:
    """What came of booking a weekly series: the appointments made, and the weeks missed.

    A small object rather than a tuple because the caller has to tell the counselor
    both halves, and a bare ``(bookings, skipped)`` at three call sites is how one of
    them ends up reporting only the first.
    """

    def __init__(self, bookings, skipped):
        #: In diary order.
        self.bookings = bookings
        #: ``(date, reason)`` for each week that could not be booked, in order.
        self.skipped = skipped

    @property
    def first(self):
        return self.bookings[0] if self.bookings else None

    def __len__(self) -> int:
        return len(self.bookings)


def book_series(
    *,
    case,
    counselee,
    start,
    occurrences: int,
    minutes=None,
    attendance=Attendance.INDIVIDUAL,
    meeting_url="",
    created_by,
    request=None,
):
    """Book the same time every week, starting at ``start``. Returns a ``Series``.

    **Each week is a separate appointment, and that is the design.** A recurring
    arrangement in a counseling diary is not one event with a repeat rule: week
    three gets moved to the Thursday, week five is cancelled with two days' notice
    and may be chargeable, week seven is the one somebody sent their homework in for.
    All of that hangs off a Booking, so a series is ten Bookings sharing a key rather
    than a rule that has to be expanded before anything can be recorded against it.

    **A week that clashes is skipped, not fatal.** Refusing the whole series because
    the counselor already has something in week six would leave them to work out
    which week, book nine by hand, and mean the feature is only usable on an empty
    diary. What the caller gets back says which weeks were missed, and telling the
    counselor is its job.

    **The time is held in wall-clock terms.** Each week is rebuilt by combining a
    date with the counselor's local time rather than by adding seven days to the
    previous instant, so "Tuesdays at two" is still two o'clock after the clocks
    change. Adding ``timedelta(weeks=1)`` to an aware datetime would drift by an hour
    for half the year, and the counselee would be the one who noticed.
    """
    if occurrences < 1:
        raise SchedulingError(_("A series needs at least one session."))
    if occurrences > MAX_SERIES_SESSIONS:
        raise SchedulingError(
            _("A series can be at most %(limit)s sessions.") % {"limit": MAX_SERIES_SESSIONS}
        )

    zone = case.counselor.zoneinfo
    local = start.astimezone(zone)
    series_key = uuid4()

    booked, skipped = [], []
    for index in range(occurrences):
        day = local.date() + timedelta(weeks=index)
        when = datetime.combine(day, local.time()).replace(tzinfo=zone)
        try:
            booked.append(
                _insert_booking(
                    case=case,
                    counselee=counselee,
                    start=when,
                    minutes=minutes,
                    attendance=attendance,
                    meeting_url=meeting_url,
                    series_key=series_key,
                    created_by=created_by,
                    # A counselor's series is not bound by their published hours, for
                    # the same reason a single booking of theirs is not.
                    enforce_availability=False,
                    request=request,
                )
            )
        except SchedulingError as exc:
            logger.info("Week %s of a series was not booked: %s", day, exc)
            skipped.append((day, str(exc)))

    if not booked:
        # Nothing was written, so there is nothing to tell anybody about and the
        # counselor needs the reason rather than a summary of a series that is not
        # there. The first week's refusal is the one that explains it.
        raise SlotUnavailable(skipped[0][1] if skipped else None)

    notify.booking_series_scheduled(booked)
    for booking in booked:
        _push_to_google(booking)
    return Series(booked, skipped)


def set_meeting_link(booking, *, actor, meeting_url, apply_to_series=False, request=None):
    """Set, change or clear where a session is held. Returns how many rows changed.

    ``apply_to_series`` covers the ordinary case: a counselor arranges ten Tuesdays
    and is sent the room link afterwards. Only appointments that are still active and
    still ahead are touched — rewriting the link on a session that has already
    happened would edit a record of the past to no purpose, and on a cancelled one it
    would be an invitation to a meeting nobody is going to.

    The field validates itself rather than the caller being trusted. ``Field.clean``
    rather than ``Booking.full_clean``, which would also re-check the exclusion
    constraints with a query and could refuse an appointment already in the diary
    over something that has nothing to do with its link.
    """
    field = Booking._meta.get_field("meeting_url")
    try:
        meeting_url = field.clean(meeting_url, booking)
    except ValidationError as exc:
        raise SchedulingError(" ".join(str(message) for message in exc.messages)) from exc

    booking.meeting_url = meeting_url
    booking.save(update_fields=["meeting_url", "updated_at"])
    changed = [booking]

    if apply_to_series and booking.series_key is not None:
        now = timezone.now()
        siblings = (
            booking.series_appointments()
            .exclude(pk=booking.pk)
            .filter(status__in=ACTIVE_STATUSES, slot__endswith__gt=now)
        )
        for sibling in siblings:
            sibling.meeting_url = meeting_url
            sibling.save(update_fields=["meeting_url", "updated_at"])
            changed.append(sibling)

    for changed_booking in changed:
        record(
            AuditVerb.BOOKING_MEETING_LINK_SET,
            actor=actor,
            target=changed_booking,
            request=request,
            case_id=changed_booking.case_id,
            starts_at=changed_booking.starts_at.isoformat(),
            # Whether there is now a link, and never the link. See _insert_booking.
            is_virtual=bool(meeting_url),
            # So the trail shows that one action changed six appointments rather than
            # six unexplained edits a second apart.
            series=str(changed_booking.series_key) if changed_booking.series_key else None,
        )
    for changed_booking in changed:
        _push_to_google(changed_booking)
    return len(changed)


def confirm(booking, *, actor, request=None):
    """Accept a requested appointment.

    Idempotent: confirming something already confirmed is a double-clicked button,
    not an error worth showing anyone.
    """
    if booking.status == BookingStatus.CONFIRMED:
        return booking
    if booking.status != BookingStatus.REQUESTED:
        raise SchedulingError(_("Only a requested appointment can be confirmed."))

    booking.status = BookingStatus.CONFIRMED
    booking.save(update_fields=["status", "updated_at"])
    record(
        AuditVerb.BOOKING_CONFIRMED,
        actor=actor,
        target=booking,
        request=request,
        case_id=booking.case_id,
        starts_at=booking.starts_at.isoformat(),
    )
    notify.booking_confirmed(booking)
    _push_to_google(booking)
    return booking


def cancel(booking, *, actor, reason="", request=None):
    """Release an appointment's place in the diary.

    Cancelled rather than deleted, and ``was_late_cancellation`` is recorded now
    rather than worked out later from timestamps: the notice period can be changed
    in a counselor's settings, and an invoice raised next month must reflect the
    rule that was in force on the day.
    """
    if not booking.is_active:
        raise SchedulingError(_("That appointment is not active."))

    window = BookingWindow(booking.counselor)
    now = timezone.now()

    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now
    booking.cancelled_by = actor
    booking.cancellation_reason = reason[:200]
    booking.was_late_cancellation = booking.hours_until(now=now) < window.notice_hours
    booking.save(
        update_fields=[
            "status",
            "cancelled_at",
            "cancelled_by",
            "cancellation_reason",
            "was_late_cancellation",
            "updated_at",
        ]
    )
    record(
        AuditVerb.BOOKING_CANCELLED,
        actor=actor,
        target=booking,
        request=request,
        case_id=booking.case_id,
        starts_at=booking.starts_at.isoformat(),
        late=booking.was_late_cancellation,
        # The reason is the counselee's words, so it stays out of the trail's
        # metadata. Whether one was given is the auditable fact.
        reason_given=bool(reason),
    )
    notify.booking_cancelled(booking, cancelled_by=actor)
    _push_to_google(booking)
    # A cancellation with proper notice raises nothing. A late one is chargeable if
    # the ministry has set a rate for it, which is the fee schedule's decision and
    # not this module's — see _raise_session_record.
    _raise_session_record(booking, actor=actor, request=request)
    return booking


def reschedule(booking, *, actor, start, minutes=None, request=None):
    """Move an appointment, keeping one row.

    One row rather than cancel-and-rebook, so the appointment's history — who
    asked for it, what note came with it — stays attached to the thing that
    actually happens. The exclusion constraint still applies to the new interval.
    """
    if not booking.is_active:
        raise SchedulingError(_("That appointment is not active."))

    previous = booking.starts_at
    minutes = minutes or booking.duration_minutes
    booking.slot = Booking.range_for(start, minutes)
    _validate(booking)

    try:
        with transaction.atomic():
            booking.save(update_fields=["slot", "updated_at"])
    except IntegrityError as exc:
        logger.info("Reschedule refused by a constraint: %s", exc)
        # The instance still carries the new slot, so a caller that renders the
        # booking after catching this would show a time it does not have.
        booking.refresh_from_db(fields=["slot"])
        raise _refusal_for(exc) from exc
    except OperationalError as exc:
        if not _is_booking_race(exc):
            raise
        logger.info("Reschedule lost a deadlock against a simultaneous booking: %s", exc)
        booking.refresh_from_db(fields=["slot"])
        raise SlotUnavailable() from exc

    # The reminder is about a time that no longer applies, so the appointment
    # becomes eligible for another one.
    if booking.reminder_sent_at is not None:
        booking.reminder_sent_at = None
        booking.save(update_fields=["reminder_sent_at", "updated_at"])

    record(
        AuditVerb.BOOKING_RESCHEDULED,
        actor=actor,
        target=booking,
        request=request,
        case_id=booking.case_id,
        previous_start=previous.isoformat(),
        starts_at=booking.starts_at.isoformat(),
        minutes=minutes,
    )
    notify.booking_rescheduled(booking, previous_start=previous)
    _push_to_google(booking)
    return booking


def mark_completed(booking, *, actor, note=None, request=None):
    """Record that the session happened. The v3 invoice is raised from this."""
    return _close_out(
        booking,
        status=BookingStatus.COMPLETED,
        verb=AuditVerb.BOOKING_COMPLETED,
        actor=actor,
        note=note,
        request=request,
    )


def mark_no_show(booking, *, actor, note=None, request=None):
    """Record that nobody came. Kept distinct from a cancellation and from a
    completed session, because the three are billed differently."""
    return _close_out(
        booking,
        status=BookingStatus.NO_SHOW,
        verb=AuditVerb.BOOKING_NO_SHOW,
        actor=actor,
        note=note,
        request=request,
    )


def _close_out(booking, *, status, verb, actor, note, request):
    if booking.status not in (*ACTIVE_STATUSES, BookingStatus.COMPLETED, BookingStatus.NO_SHOW):
        raise SchedulingError(_("That appointment has been cancelled."))
    if not booking.is_in_the_past:
        raise SchedulingError(_("That appointment has not happened yet."))

    fields = ["status", "updated_at"]
    booking.status = status
    if note is not None:
        booking.counselor_note = note
        fields.append("counselor_note")
    booking.save(update_fields=fields)
    record(
        verb,
        actor=actor,
        target=booking,
        request=request,
        case_id=booking.case_id,
        starts_at=booking.starts_at.isoformat(),
        # Never the note itself. It is counseling content and the trail is read by
        # people who are not entitled to it.
        note_recorded=bool(note),
    )
    _raise_session_record(booking, actor=actor, request=request)
    return booking


def _raise_session_record(booking, *, actor, request) -> None:
    """Copy a closed-out booking's outcome into the billing record.

    The one place scheduling reaches into billing, and it reaches in one direction:
    billing reads bookings, scheduling never reads invoices. The import is local
    because ``apps.billing.services`` imports scheduling's models — a module-level
    import here would be a cycle at startup.

    Wrapped, and deliberately so. This runs after the appointment's outcome has been
    written, the audit row recorded, and the calendar updated, so an exception
    escaping would report a failure for something that has already happened and would
    leave a counselor unable to close out a session because a fee row was malformed.
    An appointment recorded but not billed is recoverable — it shows up on the billing
    page and the office can raise it by hand. An appointment that could not be marked
    as held is not.
    """
    from apps.billing import services as billing

    try:
        billing.session_for_booking(booking, actor=actor, request=request)
    except Exception:
        logger.exception(
            "Could not raise a billing record for booking %s (%s).", booking.pk, booking.status
        )


def set_counselor_note(booking, *, actor, note, request=None):
    """The counselor's own record of the session. Never shown to a counselee."""
    booking.counselor_note = note
    booking.save(update_fields=["counselor_note", "updated_at"])
    record(
        AuditVerb.BOOKING_NOTE_UPDATED,
        actor=actor,
        target=booking,
        request=request,
        case_id=booking.case_id,
        length=len(note),
    )
    return booking


def _validate(booking) -> None:
    """Run the model's own checks, converting the failure into a refusal.

    ``full_clean`` is what enforces "the counselor carries the case" and "the
    counselee is currently on it", and it excludes no field, so one added later is
    validated without this function being remembered.

    ``validate_constraints=False`` is the load-bearing argument. Django would
    otherwise SELECT to see whether the exclusion constraints hold, which is
    exactly the read-then-write race this module refuses to rely on — and it
    reports a violation by quoting the Postgres constraint name, which is not a
    sentence to show a counselee. The database enforces the constraints where they
    cannot be raced; ``_refusal_for`` turns the refusal into English.
    """
    try:
        booking.full_clean(validate_constraints=False)
    except ValidationError as exc:
        raise NotBookable(" ".join(str(message) for message in exc.messages)) from exc


# --- reminders ------------------------------------------------------------


def due_reminders(*, within_hours=24, now=None):
    """Confirmed appointments starting soon that have not been reminded about.

    Requested-but-unconfirmed appointments are excluded on purpose: reminding
    somebody about a time the counselor has not agreed to would be worse than
    saying nothing.
    """
    now = now or timezone.now()
    return (
        Booking.objects.filter(
            status=BookingStatus.CONFIRMED,
            reminder_sent_at__isnull=True,
            slot__startswith__gt=now,
            slot__startswith__lte=now + timedelta(hours=within_hours),
        )
        .select_related("counselor", "counselee", "case")
        .order_by("slot")
    )


def send_reminder(booking, *, now=None) -> bool:
    """Send one reminder, returning whether this caller sent it.

    Claimed with a conditional UPDATE before the mail goes out, the same shape as
    ``LoginToken.consume``: two overlapping cron runs would otherwise both pass a
    ``reminder_sent_at is None`` check and remind the counselee twice. Marking
    before sending means a crashed send loses a reminder rather than repeating
    one, which is the right way round for something that arrives at 6am.
    """
    now = now or timezone.now()
    claimed = Booking.objects.filter(pk=booking.pk, reminder_sent_at__isnull=True).update(
        reminder_sent_at=now, updated_at=now
    )
    if not claimed:
        return False

    booking.reminder_sent_at = now
    notify.booking_reminder(booking)
    record(
        AuditVerb.BOOKING_REMINDER_SENT,
        actor=None,
        target=booking,
        case_id=booking.case_id,
        starts_at=booking.starts_at.isoformat(),
    )
    return True
