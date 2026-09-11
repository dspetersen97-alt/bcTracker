"""
Appointment emails.

Two rules govern everything in this file.

**Nothing counseling-related goes in an email.** A reminder says when and with
whom, and never the note the counselee wrote when booking or the counselor's own
note. Mail leaves our control the moment it is sent — it sits in an inbox on a
shared family computer, gets forwarded, gets indexed by whatever the recipient's
provider indexes. The appointment time is the minimum needed for the mail to do
its job, and the case label is deliberately absent too: "Ashford — marriage" in a
subject line is a disclosure to anyone who glances at a phone.

**A failed send never breaks the action.** Every function here logs and swallows.
Workspace SMTP has a daily cap and gives no bounce reporting, so failures are
expected and invisible; an appointment that exists must not be rolled back because
the confirmation email could not go out. The audit trail records that the booking
happened, and the log records that the mail did not.

Times are formatted per recipient rather than rendered by the template, because
these are sent from a management command where no request has activated a
timezone, and a reminder in the wrong zone is worse than no reminder.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.mail import from_address

logger = logging.getLogger(__name__)


def _when(moment, zone) -> str:
    """An appointment time written out in one person's own zone."""
    local = moment.astimezone(zone)
    # %-I is not portable to Windows, so the leading zero is stripped by hand.
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%A %d %B %Y')} at {clock} {local.strftime('%Z')}"


def _send(*, template, subject, recipient, context) -> None:
    try:
        send_mail(
            subject=subject,
            message=render_to_string(f"scheduling/email/{template}.txt", context),
            from_email=from_address(),
            recipient_list=[recipient.email],
            fail_silently=False,
        )
    except Exception:
        # Deliberately broad: SMTP raises a family of errors and a DNS failure
        # raises something else again. None of them is a reason to fail the
        # booking that triggered this.
        logger.exception("Could not send %s to user %s", template, recipient.pk)
    else:
        logger.info("sent %s to user %s", template, recipient.pk)


def _attendees(booking):
    """Who should hear about this appointment.

    An individual appointment concerns one counselee. A whole-case appointment
    concerns everyone currently on the case — and only the current members, so
    someone whose membership ended stops being emailed about the family's
    sessions.
    """
    if not booking.is_joint:
        return [booking.counselee]
    return list(booking.case.counselees)


def _context(booking, recipient, **extra):
    return {
        "recipient": recipient,
        "counselor_name": booking.counselor.full_name,
        "counselee_name": booking.counselee.full_name,
        "when": _when(booking.starts_at, recipient.zoneinfo),
        "minutes": booking.duration_minutes,
        "is_joint": booking.is_joint,
        # reverse() rather than a literal path, so the route's shape stays the
        # URLconf's business. An appointment is addressed by its public id — see
        # apps/core/ids.py — and this link is the one a counselee is most likely to
        # keep in a mailbox for weeks.
        "url": settings.SITE_BASE_URL
        + reverse("scheduling:detail", kwargs={"public_id": booking.public_id}),
        "site_url": settings.SITE_BASE_URL,
        **extra,
    }


def booking_created(booking) -> None:
    """Tell the counselor somebody has asked for a time, or the counselee that
    their counselor has put one in the diary.

    Which way round depends on who booked, because the other party is the one who
    does not know yet.
    """
    if booking.created_by_id == booking.counselor_id:
        for attendee in _attendees(booking):
            _send(
                template="booking_scheduled",
                subject="Your counselor has scheduled an appointment",
                recipient=attendee,
                context=_context(booking, attendee),
            )
        return

    _send(
        template="booking_requested",
        subject="An appointment has been requested",
        recipient=booking.counselor,
        context=_context(booking, booking.counselor),
    )


def booking_confirmed(booking) -> None:
    for attendee in _attendees(booking):
        _send(
            template="booking_confirmed",
            subject="Your appointment is confirmed",
            recipient=attendee,
            context=_context(booking, attendee),
        )


def booking_cancelled(booking, *, cancelled_by) -> None:
    """Tell everyone except the person who just did it.

    Emailing the canceller their own cancellation is noise, and on a joint
    appointment it is noise sent to someone who is already looking at the screen
    that says it worked.
    """
    for person in [booking.counselor, *_attendees(booking)]:
        if person.pk == cancelled_by.pk:
            continue
        _send(
            template="booking_cancelled",
            subject="An appointment has been cancelled",
            recipient=person,
            context=_context(booking, person, cancelled_by_staff=cancelled_by.is_ministry_staff),
        )


def booking_rescheduled(booking, *, previous_start) -> None:
    for person in [booking.counselor, *_attendees(booking)]:
        _send(
            template="booking_rescheduled",
            subject="An appointment has been moved",
            recipient=person,
            context=_context(booking, person, previously=_when(previous_start, person.zoneinfo)),
        )


def booking_reminder(booking) -> None:
    for attendee in _attendees(booking):
        _send(
            template="booking_reminder",
            subject="A reminder about your appointment",
            recipient=attendee,
            context=_context(booking, attendee),
        )
