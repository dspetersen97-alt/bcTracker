"""
Object-level permissions for scheduling.

The querysets in models.py answer "may this appear in my diary". These answer
"may this actor do this", which is what every write needs. Both apply.

``financial_admin`` is the interesting role here, and the only place in the
application where it is granted a read it is denied for documents. Billing has to
know a session took place in order to invoice it, so ``view_booking`` includes it.
It is absent from ``view_booking_note`` — the counselee's request note and the
counselor's session note are counseling content, and the distinction between "an
appointment happened" and "here is what it was about" is exactly the line the
ministry drew.
"""

import rules

from apps.accounts.rules import is_admin, is_counselee, is_counselor, is_financial_admin
from apps.counseling.rules import is_case_counselor, is_case_member


@rules.predicate
def is_the_booked_counselor(user, booking):
    if booking is None:
        return False
    return booking.counselor_id == user.pk


@rules.predicate
def is_the_booked_counselee(user, booking):
    if booking is None:
        return False
    return booking.counselee_id == user.pk


@rules.predicate
def is_expected_at_the_appointment(user, booking):
    """A current member of the case, for an appointment the whole case attends.

    Not simply "on the case": an individual appointment on a family case belongs
    to one person, and the sibling who is not attending has no business seeing it.
    """
    if booking is None:
        return False
    return booking.is_joint and is_case_member(user, booking.case)


@rules.predicate
def booking_is_still_active(user, booking):
    if booking is None:
        return False
    return booking.is_active


@rules.predicate
def booking_has_happened(user, booking):
    if booking is None:
        return False
    return booking.is_in_the_past


@rules.predicate
def is_within_the_notice_period(user, booking):
    """True when the appointment is too close to cancel without it counting late.

    Note what this does *not* do: it does not block the cancellation. A counselee
    who cannot come should always be able to say so, and the ministry would rather
    know than have somebody not turn up. The flag is recorded and v3 decides what
    it costs.
    """
    from apps.scheduling.services import BookingWindow

    if booking is None:
        return False
    return booking.hours_until() < BookingWindow(booking.counselor).notice_hours


@rules.predicate
def owns_the_availability(user, rule):
    if rule is None:
        return False
    return rule.counselor_id == user.pk


# --- office hours ---------------------------------------------------------

# A counselor's own office hours, and nobody else's — not even an administrator's.
# When one person is willing to work is theirs to state; an admin who needs a
# closure added can ask, and a case can be reassigned if they will not.
rules.add_perm("scheduling.manage_own_availability", is_counselor)
rules.add_perm("scheduling.change_availabilityrule", is_counselor & owns_the_availability)
rules.add_perm("scheduling.delete_availabilityrule", is_counselor & owns_the_availability)


# --- the Google connection ------------------------------------------------

# Connecting a calendar. Counselor only, and unlike everything else here an admin
# is excluded rather than included — deliberately.
#
# Two reasons. It is technically impossible: the consent flow runs in the
# counselor's own browser against their own Google account, so an admin has
# nothing to authorize. And it should stay impossible: an admin who could arrange
# where a counselor's appointment times are sent could route them to a calendar
# the counselor does not read, or does not know about.
rules.add_perm("scheduling.manage_own_google_calendar", is_counselor)


# --- bookings -------------------------------------------------------------

# Reading one appointment. financial_admin included; see the module docstring.
rules.add_perm(
    "scheduling.view_booking",
    is_admin
    | is_financial_admin
    | is_the_booked_counselor
    | is_the_booked_counselee
    | is_expected_at_the_appointment,
)

# The notes attached to an appointment. The counselor who holds the case, and an
# admin. Not the counselee — the counselor's note is written about them, not to
# them — and emphatically not billing.
rules.add_perm("scheduling.view_booking_note", is_admin | is_the_booked_counselor)

# The diary index. No object, so this is about the role, and the role that is
# missing is the point: financial_admin reaching a list of every appointment in
# the ministry would be a caseload-shaped disclosure that billing does not need.
# What billing needs is per-case, and it gets that through the case pages.
rules.add_perm("scheduling.view_diary", is_admin | is_counselor | is_counselee)

# Booking into a case. Checked against a *Case*, since there is no booking yet.
rules.add_perm("scheduling.add_booking", is_admin | is_case_counselor | is_case_member)

# Confirming a request. The counselor's decision: it is their diary, and a
# counselee confirming their own request would make the status meaningless.
rules.add_perm(
    "scheduling.confirm_booking",
    (is_admin | is_the_booked_counselor) & booking_is_still_active,
)

# Cancelling. Everyone expected in the room, plus the counselor and an admin.
# Deliberately not gated on the notice period — see is_within_the_notice_period.
rules.add_perm(
    "scheduling.cancel_booking",
    (is_admin | is_the_booked_counselor | is_the_booked_counselee | is_expected_at_the_appointment)
    & booking_is_still_active,
)

# Moving an appointment. Counselor and admin only: a counselee cancels and books
# again, which goes through the availability rules, whereas rescheduling does not.
rules.add_perm(
    "scheduling.reschedule_booking",
    (is_admin | is_the_booked_counselor) & booking_is_still_active,
)

# Recording what became of a session — held, or missed. The counselor's own
# record, and the fact a v3 invoice is raised from, so an admin may correct it.
rules.add_perm(
    "scheduling.record_outcome",
    (is_admin | is_the_booked_counselor) & booking_has_happened,
)

# Writing the counselor's note. The counselor alone; an admin may read one (see
# view_booking_note) but has no business authoring a session note they were not at.
rules.add_perm("scheduling.change_booking_note", is_the_booked_counselor)

# Deleting an appointment is not a permission anyone holds. Appointments are
# cancelled, and a cancellation — especially a late one — is part of the record.
rules.add_perm("scheduling.delete_booking", rules.predicate(lambda user, booking: False))
