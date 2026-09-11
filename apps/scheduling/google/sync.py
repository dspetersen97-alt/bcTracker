"""
Pushing appointments into a counselor's Google calendar.

The reconciliation is deliberately dumb, and dumb is the requirement. For each
booking in the window: an active one should have an event matching what bcTracker
believes, and an inactive one should have no event. There is no merge, no
three-way diff, and no attempt to interpret a change made in Google. bcTracker
wins, always.

That is what makes the failure modes tolerable. The worst thing this module can do
is overwrite an edit a counselor made in Google — annoying, and recoverable by
editing it in bcTracker instead. The alternative, a two-way sync, can move or
delete a real appointment on the strength of a misread etag, and the person who
finds out is a counselee standing at a locked door.

**Nothing here may raise into a booking.** ``push_booking`` swallows everything
except a programming error, because an appointment that was successfully made and
emailed must not be undone by Google being unreachable. The cron command picks up
whatever was missed; ``google_synced_at`` is how it knows.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.scheduling.google.client import BOOKING_ID_PROPERTY, CalendarClient
from apps.scheduling.google.errors import (
    AuthorizationLost,
    EventGone,
    GoogleError,
    GoogleNotConfigured,
    TransientGoogleError,
)
from apps.scheduling.models import (
    ACTIVE_STATUSES,
    Booking,
    BookingStatus,
    GoogleCredential,
)

logger = logging.getLogger(__name__)


def credential_for(counselor) -> GoogleCredential | None:
    """The usable connection for this counselor, or None.

    None covers every reason there is nothing to do — the integration is off, the
    counselor never connected, the grant was revoked — because to every caller
    they are the same case: do not push, and do not treat it as an error.
    """
    if not settings.GOOGLE_CALENDAR_ENABLED:
        return None
    credential = GoogleCredential.objects.filter(counselor=counselor).first()
    if credential is None or not credential.is_usable:
        return None
    return credential


# --- the event body -------------------------------------------------------


def event_body(booking, *, credential) -> dict:
    """What the Google event should look like for this booking.

    Two things are absent on purpose.

    **No attendees.** Adding the counselee as a Google attendee would have Google
    email them an invitation, put a counseling appointment in whatever calendar
    their address belongs to — often a shared family or employer calendar — and
    expose their address to anyone who can see the counselor's event. The
    counselee already gets an email from bcTracker.

    **No counseling content.** Not the request note, not the session note, not the
    case label unless names are switched on. A Google calendar is outside this
    application's access model entirely: the four roles, the scoping querysets and
    the audit trail all stop at the API boundary, so the only safe amount of
    counseling content to send is none.
    """
    summary = settings.GOOGLE_EVENT_TITLE
    if credential.include_names:
        summary = f"{summary} — {booking.counselee.full_name}"

    return {
        "summary": summary,
        "description": _description(booking),
        "start": {"dateTime": booking.starts_at.isoformat()},
        "end": {"dateTime": booking.ends_at.isoformat()},
        # "tentative" for a request the counselor has not confirmed, so a glance at
        # the calendar distinguishes an appointment from an ask. Google renders it
        # with a hatched background, which is exactly the right amount of signal.
        "status": ("tentative" if booking.status == BookingStatus.REQUESTED else "confirmed"),
        # Busy, so the counselor's own free/busy — which we read back — reflects it.
        "transparency": "opaque",
        # Hidden from anyone the calendar is shared with who does not have
        # permission to see details. Defence in depth on top of the empty title.
        "visibility": "private",
        # The counselor's own calendar defaults. bcTracker sends its own reminder
        # email and has no business overriding how somebody's calendar nags them.
        "reminders": {"useDefault": True},
        "extendedProperties": {"private": {BOOKING_ID_PROPERTY: str(booking.pk)}},
        # Google would otherwise invent one and mail it to nobody.
        "guestsCanInviteOthers": False,
        "guestsCanSeeOtherGuests": False,
    }


def _description(booking) -> str:
    """A link back, and nothing that reads as a record.

    The URL carries a booking id, which is not a secret: reaching it still needs a
    bcTracker session that passes ``for_actor`` and the permission check. Worth
    including because the counselor's route from "10am on my phone" to "who is
    this and what did we agree" should be one tap.
    """
    from django.urls import reverse

    url = f"{settings.SITE_BASE_URL}{reverse('scheduling:detail', kwargs={'pk': booking.pk})}"
    return f"Booked in bcTracker.\n{url}"


# --- one booking ----------------------------------------------------------


def push_booking(booking, *, credential=None, raise_errors=False) -> bool:
    """Make Google agree with this one booking. Returns whether anything was sent.

    ``raise_errors=False`` — the default, and what every view and service uses —
    means a Google failure is logged against the connection and forgotten. The
    booking stands. The cron command retries.

    ``raise_errors=True`` is for that command, which wants to count failures and
    stop early on an authorization problem rather than hammering a dead grant once
    per booking.
    """
    credential = credential or credential_for(booking.counselor)
    if credential is None:
        return False

    try:
        _push(booking, credential)
    except GoogleNotConfigured:
        return False
    except GoogleError as exc:
        _record_failure(credential, exc)
        if raise_errors:
            raise
        return False
    return True


def _push(booking, credential) -> None:
    client = CalendarClient(credential)

    if booking.status == BookingStatus.CANCELLED:
        # The only status that removes an event. Note it is *not* "anything that is
        # not active": a completed session and a no-show are both inactive, and both
        # should stay in the calendar. Deleting them would rewrite the counselor's
        # history of where their week went, and a no-show in particular is a thing
        # somebody may need to point at later.
        if booking.google_event_id:
            _delete(booking, client)
        return

    if booking.google_event_id:
        try:
            written = client.update(
                booking.google_event_id,
                event_body(booking, credential=credential),
                etag=booking.google_etag,
            )
        except EventGone:
            # Deleted or edited in Google. bcTracker is authoritative, so the id is
            # forgotten and a fresh event inserted — a counselor tidying their
            # calendar must not silently lose the appointment.
            logger.info("Google event for booking %s is gone or changed; re-inserting.", booking.pk)
            written = client.insert(event_body(booking, credential=credential))
    else:
        written = client.insert(event_body(booking, credential=credential))

    _record_success(booking, credential, written)


def _delete(booking, client) -> None:
    try:
        client.delete(booking.google_event_id)
    except EventGone:
        # Already gone. The desired state, reached by someone else.
        pass
    Booking.objects.filter(pk=booking.pk).update(
        google_event_id="", google_etag="", google_synced_at=timezone.now()
    )
    booking.google_event_id = ""
    booking.google_etag = ""


def _record_success(booking, credential, written: dict) -> None:
    """Save what Google gave back, without touching anything else on the row.

    ``queryset.update`` rather than ``booking.save()`` on purpose: this runs after
    a booking has been created or changed, and a full save would write back
    whatever else is on the in-memory instance — including a stale field read
    before some other request changed it.
    """
    event_id = written.get("id") or booking.google_event_id
    etag = written.get("etag", "")
    now = timezone.now()
    Booking.objects.filter(pk=booking.pk).update(
        google_event_id=event_id, google_etag=etag, google_synced_at=now
    )
    booking.google_event_id = event_id
    booking.google_etag = etag
    booking.google_synced_at = now

    if credential.last_error or credential.last_synced_at is None:
        credential.last_error = ""
        credential.last_synced_at = now
        credential.save(update_fields=["last_error", "last_synced_at", "updated_at"])
    else:
        GoogleCredential.objects.filter(pk=credential.pk).update(last_synced_at=now)
        credential.last_synced_at = now


def _record_failure(credential, exc: GoogleError) -> None:
    """Log, and tell the counselor only about failures they can act on.

    A transient error is logged and nothing else: showing "sync failed" because one
    request timed out would train counselors to ignore the banner, and then the
    one that matters is ignored too.
    """
    if isinstance(exc, TransientGoogleError):
        logger.warning("Google sync will retry for counselor %s: %s", credential.counselor_id, exc)
        return

    if isinstance(exc, AuthorizationLost):
        # mark_revoked has already run inside the credentials layer, which owns the
        # message. Nothing to add here beyond the audit row.
        logger.error("Google authorization lost for counselor %s: %s", credential.counselor_id, exc)
        record(
            AuditVerb.GOOGLE_DISCONNECTED,
            actor=None,
            target=credential,
            reason="authorization_lost",
        )
        return

    logger.error("Google refused a sync for counselor %s: %s", credential.counselor_id, exc)
    message = (
        "bcTracker could not update your Google calendar. An administrator has been "
        "notified; your appointments in bcTracker are unaffected."
    )
    GoogleCredential.objects.filter(pk=credential.pk).update(last_error=message)
    credential.last_error = message


# --- a whole counselor ----------------------------------------------------


def bookings_needing_push(counselor, *, horizon_days=None):
    """Bookings whose Google state may be out of date, soonest first.

    Two groups, and the second is the one that makes the integration trustworthy:

      * active appointments never pushed, or changed since they were last pushed;
      * **cancelled appointments that still hold an event id** — the case where
        something has to be *removed* from a calendar. Missing this is how a
        counselor keeps a cancelled session on their phone and turns up for it.

    Cancelled, specifically — not "inactive". A completed session and a no-show
    are both inactive and both stay in the calendar, because they are a record of
    where the week went.

    Past appointments are left alone. Rewriting last month's calendar serves
    nobody, and it would mean every cron tick doing work proportional to the whole
    history.
    """
    horizon = horizon_days if horizon_days is not None else settings.GOOGLE_SYNC_HORIZON_DAYS
    now = timezone.now()
    window_end = now + timedelta(days=horizon)

    # ``updated_at`` is auto_now, and every successful push writes google_synced_at
    # through ``queryset.update``, which does *not* touch it. So "synced before it
    # was last changed" is an exact test for a stale event, and a push does not
    # make the row look stale again the moment it succeeds.
    stale = Q(google_synced_at__isnull=True) | Q(google_synced_at__lt=F("updated_at"))
    upcoming_active = Q(status__in=ACTIVE_STATUSES, slot__endswith__gte=now) & stale
    needs_removal = ~Q(google_event_id="") & Q(status=BookingStatus.CANCELLED)

    return (
        Booking.objects.filter(counselor=counselor, slot__startswith__lte=window_end)
        .filter(upcoming_active | needs_removal)
        .select_related("counselee", "case")
        .order_by("slot")
    )


def sync_counselor(counselor, *, horizon_days=None) -> dict:
    """Reconcile one counselor's calendar. Returns a small tally.

    Stops on ``AuthorizationLost``: with a dead grant every remaining booking
    would fail identically, and a hundred failed refresh attempts against Google
    is how an application earns a rate limit.
    """
    credential = credential_for(counselor)
    if credential is None:
        return {"skipped": True, "pushed": 0, "failed": 0}

    tally = {"skipped": False, "pushed": 0, "failed": 0, "authorization_lost": False}
    for booking in bookings_needing_push(counselor, horizon_days=horizon_days):
        try:
            if push_booking(booking, credential=credential, raise_errors=True):
                tally["pushed"] += 1
        except AuthorizationLost:
            tally["authorization_lost"] = True
            break
        except GoogleError:
            tally["failed"] += 1
    return tally


def sync_all(*, horizon_days=None) -> dict:
    """Every connected counselor. What the cron command runs."""
    if not settings.GOOGLE_CALENDAR_ENABLED:
        return {"counselors": 0, "pushed": 0, "failed": 0, "disconnected": 0}

    totals = {"counselors": 0, "pushed": 0, "failed": 0, "disconnected": 0}
    connections = GoogleCredential.objects.filter(revoked_at__isnull=True).select_related(
        "counselor"
    )
    for credential in connections:
        tally = sync_counselor(credential.counselor, horizon_days=horizon_days)
        if tally["skipped"]:
            continue
        totals["counselors"] += 1
        totals["pushed"] += tally["pushed"]
        totals["failed"] += tally["failed"]
        totals["disconnected"] += 1 if tally.get("authorization_lost") else 0
    return totals


# --- reading free/busy back -----------------------------------------------


def busy_from_google(counselor, *, start, end) -> list[tuple]:
    """The counselor's other Google commitments, as bare intervals.

    Returns ``[]`` for every failure, and that is the deliberate direction to fail
    in. Treating an unreachable Google as "completely busy" would empty the
    booking page and stop counselees booking at all; treating it as "free" risks
    offering a slot the counselor has something else in, which they can decline.
    An empty booking page is the worse outcome, and it is the one nobody
    diagnoses.
    """
    credential = credential_for(counselor)
    if credential is None or not credential.block_slots_from_calendar:
        return []
    try:
        return CalendarClient(credential).freebusy(start=start, end=end)
    except (GoogleError, ValueError) as exc:
        logger.warning("Could not read Google free/busy for counselor %s: %s", counselor.pk, exc)
        return []
