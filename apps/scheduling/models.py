"""
Office hours, and the appointments booked into them.

Three models describe the diary, and the split between them is the point:

  * ``AvailabilityRule`` is a recurring weekly window in the counselor's *local*
    time — "Tuesdays, 9am to noon". Local, not UTC, because "9am" is what the
    counselor means and it stays 9am across a daylight-saving change.
  * ``AvailabilityOverride`` is a single date that departs from the pattern: a
    closure, or an extra window.
  * ``Booking`` is one appointment, stored as a **tstzrange** so Postgres itself
    can refuse an overlap.

Nothing here computes free slots. That is ``slots.py``, a pure function over
these rows, so the awkward parts — daylight saving, minimum notice, an
appointment already in the diary — are testable without a database.

A fourth, ``GoogleCredential``, is not about the diary at all: it holds one
counselor's authorization to write into their Google calendar. It lives here
because it is worthless without a Booking to push, and its refresh token is
sealed with the document envelope scheme — see its own docstring.

Double booking is prevented by the exclusion constraints below rather than by a
check in a view. Two counselees clicking the same 10am at the same moment is a
race that no amount of application code wins reliably, and the constraint holds
even against code written later that forgets to look.
"""

from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.db.backends.postgresql.psycopg_any import DateTimeTZRange
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role
from apps.core.dates import org_today
from apps.core.models import PublicIdModel, TimeStampedModel
from apps.core.scoping import ActorScopedQuerySet, CaseScopedQuerySet


class Weekday(models.IntegerChoices):
    """Monday is 0, matching ``datetime.date.weekday()``.

    Python's own numbering rather than Postgres's or ISO's, because every
    comparison in slots.py is against ``date.weekday()`` and a translation layer
    would be one more place to get an off-by-one wrong.
    """

    MONDAY = 0, _("Monday")
    TUESDAY = 1, _("Tuesday")
    WEDNESDAY = 2, _("Wednesday")
    THURSDAY = 3, _("Thursday")
    FRIDAY = 4, _("Friday")
    SATURDAY = 5, _("Saturday")
    SUNDAY = 6, _("Sunday")


class BookingStatus(models.TextChoices):
    REQUESTED = "requested", _("Requested")
    CONFIRMED = "confirmed", _("Confirmed")
    CANCELLED = "cancelled", _("Cancelled")
    COMPLETED = "completed", _("Completed")
    NO_SHOW = "no_show", _("Did not attend")


#: Statuses that hold a place in the diary. Everything else has released it, which
#: is why the exclusion constraints below are conditional on this set.
ACTIVE_STATUSES = (BookingStatus.REQUESTED, BookingStatus.CONFIRMED)


#: The longest weekly series a counselor may book in one go. A term of counseling
#: is a dozen sessions or so; the cap is here because "how many sessions?" is a
#: number typed into a box, and a slip of the finger should not put four hundred
#: appointments in a diary and send an email about them.
MAX_SERIES_SESSIONS = 52


def validate_meeting_link(value):
    """A meeting link has to be ``https``, and only that.

    ``URLField`` already refuses ``javascript:`` — its validator allows only
    http, https, ftp and ftps — so this is not what stops a link from becoming a
    script. It stops the other two:

      * **http**, because the page it is rendered on is served over TLS and a
        counseling session joined over a plain connection is a counseling session
        anybody on the network can join. Every provider a ministry would use
        offers https, so nothing legitimate is refused.
      * **ftp**, which cannot possibly be a meeting and would only ever be a
        pasted mistake or an attempt to see what the field accepts.

    Validated on the model rather than only on the form, because the form is not
    the only way a row is written — the recurring-series service copies the value
    onto every appointment it creates, and ``full_clean`` is what makes that copy
    honest.
    """
    if not value:
        return
    if urlsplit(value).scheme != "https":
        raise ValidationError(_("A meeting link has to start with https:// ."))


class Attendance(models.TextChoices):
    """Who is expected in the room.

    The same shape as ``Document.visibility``, and for the same reason: on a
    couple's or family case, one member's appointment is not automatically the
    others' business. An individual appointment is visible to the counselee it
    belongs to and to the counselor; a whole-case appointment is visible to
    everyone currently on the case.
    """

    INDIVIDUAL = "individual", _("Just me and my counselor")
    WHOLE_CASE = "whole_case", _("Everyone on the case")


# --- availability ---------------------------------------------------------


class CounselorOwnedQuerySet(ActorScopedQuerySet):
    """Office hours: a counselor's own, and readable by those who must book into them.

    A counselee sees the rules of the counselors carrying their current cases,
    because that is what generating bookable slots requires. They see nobody
    else's, so office hours cannot be used to enumerate the ministry's staff or
    to work out who is quiet this week.

    ``financial_admin`` gets nothing. When someone is in the office is not
    billing data; what gets billed is a session that happened.
    """

    def scope_for_admin(self, user):
        return self

    def scope_for_counselor(self, user):
        return self.filter(counselor=user)

    def scope_for_counselee(self, user):
        return self.filter(
            counselor__cases__members__counselee=user,
            counselor__cases__members__ended_on__isnull=True,
            counselor__cases__deleted_at__isnull=True,
        ).distinct()


class AvailabilityRule(PublicIdModel, TimeStampedModel):
    """A recurring weekly window of office hours, in the counselor's local time."""

    counselor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="availability_rules",
        limit_choices_to={"role": Role.COUNSELOR, "is_active": True},
    )
    weekday = models.IntegerField(choices=Weekday.choices)
    # Local wall-clock times. Storing UTC here would be wrong, not merely
    # awkward: 9am Eastern is 13:00 UTC in January and 14:00 UTC in July, and the
    # counselor's answer to "when do you see people" does not change in March.
    start_time = models.TimeField(help_text=_("In your own timezone."))
    end_time = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(
        default=60,
        help_text=_("How long each appointment in this window is."),
    )

    # org_today for the same reason case dates use it: a stored date must not
    # depend on the timezone active when the row was written. See
    # apps/core/dates.py.
    effective_from = models.DateField(default=org_today)
    effective_to = models.DateField(
        null=True,
        blank=True,
        help_text=_("Leave blank while these hours are open-ended."),
    )

    objects = models.Manager.from_queryset(CounselorOwnedQuerySet)()

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F("start_time")),
                name="availability_window_ends_after_it_starts",
            ),
            models.CheckConstraint(
                condition=models.Q(slot_minutes__gte=15) & models.Q(slot_minutes__lte=480),
                name="availability_slot_length_is_plausible",
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="availability_effective_range_is_ordered",
            ),
            # Exact duplicates only. Overlapping windows are allowed on purpose —
            # "9–12 in hour slots, 13–14 for a longer session" is one weekday with
            # two rules, and slots.py deduplicates whatever they generate.
            models.UniqueConstraint(
                fields=["counselor", "weekday", "start_time", "end_time", "effective_from"],
                name="uniq_availability_window",
            ),
        ]
        indexes = [models.Index(fields=["counselor", "weekday"])]

    def __str__(self) -> str:
        return f"{self.get_weekday_display()} {self.start_time:%H:%M}–{self.end_time:%H:%M}"

    def clean(self):
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": _("The window has to end after it starts.")})
        if self.counselor_id and self.counselor.role != Role.COUNSELOR:
            raise ValidationError({"counselor": _("Office hours belong to a counselor.")})

    def covers(self, day) -> bool:
        """Whether this rule is in force on ``day`` — a plain date, not a datetime."""
        if self.weekday != day.weekday():
            return False
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to


class AvailabilityOverride(PublicIdModel, TimeStampedModel):
    """One date that departs from the weekly pattern.

    Three shapes, and they compose:

      * ``is_available=False`` with no times — closed all day. A holiday.
      * ``is_available=False`` with times — closed for part of the day. A meeting.
      * ``is_available=True`` with times — open outside the usual pattern.
    """

    counselor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="availability_overrides",
        limit_choices_to={"role": Role.COUNSELOR, "is_active": True},
    )
    date = models.DateField()
    is_available = models.BooleanField(
        default=False,
        help_text=_("Unticked closes the time; ticked opens it."),
    )
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    # Shown to nobody but the counselor. "Hospital" or a counselee's name would
    # both be disclosures if this appeared on a booking page.
    reason = models.CharField(max_length=120, blank=True)

    objects = models.Manager.from_queryset(CounselorOwnedQuerySet)()

    class Meta:
        ordering = ["date", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(start_time__isnull=True, end_time__isnull=True)
                    | models.Q(start_time__isnull=False, end_time__isnull=False)
                ),
                name="override_has_both_times_or_neither",
            ),
            models.CheckConstraint(
                condition=models.Q(start_time__isnull=True)
                | models.Q(end_time__gt=models.F("start_time")),
                name="override_window_ends_after_it_starts",
            ),
            # An all-day *opening* would mean midnight to midnight, which is never
            # what anyone means. Extra hours are a window.
            models.CheckConstraint(
                condition=models.Q(is_available=False) | models.Q(start_time__isnull=False),
                name="extra_availability_needs_a_window",
            ),
            # Partial constraint: one all-day closure per date, since a second
            # would be meaningless. Postgres treats NULLs as distinct, so an
            # ordinary unique constraint would not catch this.
            models.UniqueConstraint(
                fields=["counselor", "date"],
                condition=models.Q(start_time__isnull=True),
                name="uniq_all_day_override_per_date",
            ),
        ]
        indexes = [models.Index(fields=["counselor", "date"])]

    def __str__(self) -> str:
        if self.start_time is None:
            return f"{self.date}: closed"
        state = "open" if self.is_available else "closed"
        return f"{self.date}: {state} {self.start_time:%H:%M}–{self.end_time:%H:%M}"

    def clean(self):
        if (self.start_time is None) != (self.end_time is None):
            raise ValidationError(_("Give both a start and an end time, or neither."))
        if self.is_available and self.start_time is None:
            raise ValidationError(
                {"start_time": _("Extra availability needs a start and end time.")}
            )
        if self.start_time and self.end_time and self.end_time <= self.start_time:
            raise ValidationError({"end_time": _("The window has to end after it starts.")})

    @property
    def is_all_day(self) -> bool:
        return self.start_time is None


# --- bookings -------------------------------------------------------------


class BookingQuerySet(CaseScopedQuerySet):
    case_path = "case"

    def active(self):
        """Bookings holding a place in the diary."""
        return self.filter(status__in=ACTIVE_STATUSES)

    def upcoming(self, *, now=None):
        now = now or timezone.now()
        return self.filter(slot__endswith__gt=now)

    def past(self, *, now=None):
        now = now or timezone.now()
        return self.filter(slot__endswith__lte=now)

    def scope_for_counselee(self, user):
        """Their own appointments, plus anything booked for the whole case.

        The same rule as documents, for the same reason: on a family case, that a
        sibling is coming in on Thursday is not something to disclose by way of a
        calendar. A joint session is visible to everyone expected to be in it.
        """
        mine_or_shared = models.Q(counselee=user) | models.Q(attendance=Attendance.WHOLE_CASE)
        return super().scope_for_counselee(user).filter(mine_or_shared)

    def scope_for_financial_admin(self, user):
        """Kept, unlike documents.

        An appointment that happened is what gets invoiced in v3, so billing has
        to be able to see that one did. What billing must not see is *why* — the
        counselee's note is behind ``scheduling.view_booking_note``, and no
        financial_admin template renders it.
        """
        return self


class BookingManager(models.Manager.from_queryset(BookingQuerySet)):
    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class Booking(PublicIdModel, TimeStampedModel):
    """One appointment.

    Not soft-deleted: an appointment is cancelled rather than removed, because a
    late cancellation is itself a fact the ministry may bill for and the counselor
    needs to see that it happened.
    """

    counselor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings_as_counselor",
        limit_choices_to={"role": Role.COUNSELOR},
    )
    case = models.ForeignKey("counseling.Case", on_delete=models.PROTECT, related_name="bookings")
    counselee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings_as_counselee",
        limit_choices_to={"role": Role.COUNSELEE},
        help_text=_("Whose appointment this is. For a joint session, whoever booked it."),
    )
    attendance = models.CharField(
        max_length=20,
        choices=Attendance.choices,
        default=Attendance.INDIVIDUAL,
    )

    # A tstzrange, stored UTC. A range rather than a start plus a duration
    # because the exclusion constraints below are the only race-free way to stop
    # a double booking, and they need something Postgres can compare.
    slot = DateTimeRangeField()
    status = models.CharField(
        max_length=20,
        choices=BookingStatus.choices,
        default=BookingStatus.REQUESTED,
        db_index=True,
    )

    # Where a virtual session happens. Not counseling content — it is a room, not
    # what is said in it — so unlike the notes it is shown to everybody expected at
    # the appointment and put in the email about it, which is the whole point: a
    # counselee should not have to sign in to find out how to join.
    #
    # A stored link and not a generated one. bcTracker does not run a meeting
    # service and will not become an OAuth client of one to create rooms on a
    # counselor's behalf; the counselor pastes the link their own provider gave
    # them, and that keeps this feature to a field.
    meeting_url = models.URLField(
        max_length=500,
        blank=True,
        validators=[validate_meeting_link],
        verbose_name=_("Meeting link"),
        help_text=_("Optional. A https:// link the counselee can click to join."),
    )

    # Which weekly series this appointment belongs to, if any. Shared by every
    # appointment booked in one recurring run.
    #
    # A key on each row rather than a parent table, because a series is not a thing
    # that exists in its own right: each appointment is booked, confirmed, moved,
    # cancelled and billed on its own, and one that has been moved to a Thursday is
    # still one of the ten the counselor arranged. A parent row would invite code to
    # treat the series as the unit and then have to special-case every appointment
    # that stopped matching it.
    #
    # A UUID rather than a public id: this never appears in a URL, and nothing is
    # addressed by it.
    series_key = models.UUIDField(null=True, blank=True, editable=False, db_index=True)

    # What the counselee wrote when booking. Counseling content, so it is behind
    # its own permission and never rendered for financial_admin.
    request_note = models.TextField(blank=True, help_text=_("Anything your counselor should know."))
    # The counselor's own note about the session. Never shown to a counselee.
    counselor_note = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="bookings_created",
        help_text=_("Who made the booking — the counselee, the counselor, or an admin."),
    )

    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bookings_cancelled",
    )
    cancellation_reason = models.CharField(max_length=200, blank=True)
    # Set when a cancellation arrives inside the notice period. Not a punishment:
    # it is the fact a v3 invoice needs, recorded at the time rather than
    # reconstructed from timestamps later.
    was_late_cancellation = models.BooleanField(default=False)

    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    # Google Calendar. Present from the first migration although nothing writes
    # them until the sync lands, so adding it is a code change and not a schema
    # change on a live database. The etag is what lets a later incremental sync
    # recognise an echo of our own write.
    google_event_id = models.CharField(max_length=1024, blank=True)
    google_etag = models.CharField(max_length=255, blank=True)
    google_synced_at = models.DateTimeField(null=True, blank=True)

    objects = BookingManager()

    class Meta:
        ordering = ["-slot"]
        constraints = [
            # No two live appointments for one counselor may overlap. This is the
            # whole double-booking defence; the view catches the IntegrityError
            # and re-renders "that time has just been taken".
            ExclusionConstraint(
                name="no_overlapping_bookings_per_counselor",
                expressions=[
                    ("slot", RangeOperators.OVERLAPS),
                    ("counselor", RangeOperators.EQUAL),
                ],
                condition=models.Q(status__in=ACTIVE_STATUSES),
            ),
            # And a counselee cannot be in two places at once — which is a
            # different mistake, usually a counselor booking over something the
            # counselee arranged with someone else.
            ExclusionConstraint(
                name="no_overlapping_bookings_per_counselee",
                expressions=[
                    ("slot", RangeOperators.OVERLAPS),
                    ("counselee", RangeOperators.EQUAL),
                ],
                condition=models.Q(status__in=ACTIVE_STATUSES),
            ),
            models.CheckConstraint(
                condition=models.Q(slot__isempty=False)
                & models.Q(slot__lower_inf=False)
                & models.Q(slot__upper_inf=False),
                name="booking_slot_is_a_real_interval",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=BookingStatus.CANCELLED)
                | models.Q(cancelled_at__isnull=False),
                name="cancelled_booking_records_when",
            ),
        ]
        indexes = [
            models.Index(fields=["counselor", "status"]),
            models.Index(fields=["case", "-created_at"]),
            models.Index(fields=["counselee", "status"]),
            # Drives the reminder job: confirmed, starting soon, not yet reminded.
            models.Index(fields=["reminder_sent_at", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.counselee} with {self.counselor} at {self.starts_at:%Y-%m-%d %H:%M}"

    def clean(self):
        if self.case_id and self.counselor_id and self.case.counselor_id != self.counselor_id:
            raise ValidationError(
                {"counselor": _("The appointment must be with the counselor carrying the case.")}
            )
        if self.case_id and self.counselee_id:
            attending = self.case.members.filter(
                counselee_id=self.counselee_id, ended_on__isnull=True
            ).exists()
            if not attending:
                raise ValidationError(
                    {"counselee": _("That person is not currently on this case.")}
                )

    @staticmethod
    def range_for(start, minutes: int) -> DateTimeTZRange:
        """Build the half-open range for an appointment.

        Half-open — ``[start, end)`` — so a 10:00–11:00 appointment and an
        11:00–12:00 one do not count as overlapping. With inclusive bounds every
        back-to-back pair in the diary would collide.

        Here so that nothing outside this model has to know the range type; the
        psycopg import is an implementation detail of storing an interval.
        """
        return DateTimeTZRange(start, start + timedelta(minutes=minutes), bounds="[)")

    @property
    def starts_at(self):
        return self.slot.lower

    @property
    def ends_at(self):
        return self.slot.upper

    @property
    def duration_minutes(self) -> int:
        return int((self.slot.upper - self.slot.lower).total_seconds() // 60)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def is_in_the_past(self) -> bool:
        return self.ends_at <= timezone.now()

    @property
    def is_joint(self) -> bool:
        return self.attendance == Attendance.WHOLE_CASE

    @property
    def is_virtual(self) -> bool:
        return bool(self.meeting_url)

    def series_appointments(self):
        """Every appointment booked in the same recurring run as this one.

        Unscoped, and only ever used for a counselor's own writes — applying a
        meeting link to the rest of a series they arranged. A read for a counselee
        goes through ``for_actor`` like everything else; a series is not a way round
        the scoping layer, which is why this returns a queryset rather than a list
        and why no view hands it to a template.
        """
        if self.series_key is None:
            return Booking.objects.none()
        return Booking.objects.filter(series_key=self.series_key).order_by("slot")

    def hours_until(self, *, now=None) -> float:
        now = now or timezone.now()
        return (self.starts_at - now).total_seconds() / 3600

    def attendee_label_for(self, viewer) -> str:
        """Whose appointment to say this is, from ``viewer``'s side.

        The same problem as ``Document.source_label_for``, and the same answer.
        Staff and billing see the name, because whose session it was is the record
        and the invoice. A counselee sees "You" or, for a joint appointment they can
        see because the whole case attends it, a phrase that names nobody — the
        ``counselee`` column on a joint booking holds whoever arranged it, and on a
        family case that is not a fact to publish to the rest of the case.
        """
        if self.counselee_id == viewer.pk:
            return _("You")
        if viewer.role == Role.COUNSELEE:
            return _("Everyone on the case")
        return self.counselee.full_name


class GoogleCredentialQuerySet(ActorScopedQuerySet):
    """A counselor's own Google connection, and an admin's view of who has one.

    An admin sees the rows because "whose calendar has stopped syncing" is an
    administrative question, and because a counselor who has left needs their
    connection revoked by somebody else. They see no token either way — the
    plaintext is not in the row.

    Counselees and billing get nothing. Neither of them has any business knowing
    which staff calendars the ministry pushes to.
    """

    def scope_for_admin(self, user):
        return self

    def scope_for_counselor(self, user):
        return self.filter(counselor=user)


class GoogleCredential(TimeStampedModel):
    """One counselor's authorization to write to their Google calendar.

    The refresh token is the whole point of this row and it is **never stored in
    the clear**. It is sealed with the same envelope scheme as a document — a
    per-row DEK wrapped by the master key, with this row's ``storage_key`` as
    additional authenticated data — so a stolen database dump yields no calendar
    access, and a ciphertext copied from one counselor's row into another's fails
    authentication rather than silently granting the wrong calendar.

    A refresh token is a long-lived credential to a system outside this one. That
    makes it the most dangerous secret in the database after the master key
    itself: unlike a session it does not expire, and unlike a password it cannot
    be rotated by the person it belongs to without them noticing something is
    wrong. Hence the same protection as counseling documents, and hence
    ``revoke``, which drops the ciphertext rather than merely flagging the row.
    """

    counselor = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="google_credential",
    )
    # Which Google account was connected. Kept so the settings page can say
    # *whose* calendar this is: a counselor with a personal and a ministry
    # account needs to see which one they authorized, and an admin investigating
    # a failed sync needs it without a round trip to Google.
    google_email = models.EmailField()
    # Almost always "primary". A counselor who keeps counseling appointments in a
    # separate calendar can name it, which is the setting that makes the
    # integration usable for someone who shares their main calendar with family.
    calendar_id = models.CharField(max_length=255, default="primary")

    # What Google actually granted. Stored rather than assumed, because the
    # consent screen lets a person untick a scope and the resulting failure would
    # otherwise be a 403 from an API call with no explanation.
    granted_scopes = models.TextField(blank=True)

    # Whether the counselee's name appears in the Google event title.
    #
    # Off by default, and the default is the considered position. A Google
    # calendar shows up on a phone lock screen, in a laptop notification, and on
    # whatever screen is being shared in a meeting; a smart speaker will read the
    # title aloud. "Counseling appointment" reserves the time without disclosing
    # anything. A counselor who works from a private device can turn it on, but it
    # has to be their decision and it has to be visible on the settings page.
    include_names = models.BooleanField(
        default=False,
        verbose_name=_("Show counselee names in my Google calendar"),
        help_text=_(
            "Off by default. Anyone who can see your calendar — on a shared "
            "screen, or a phone notification — would see the name."
        ),
    )
    # Whether the counselor's other Google commitments hide bookable slots.
    #
    # On by default, since a counselor whose dentist appointment is not in the
    # slot list is the reason to connect at all. Off is for someone whose Google
    # calendar is full of all-day markers and low-value invitations that would
    # otherwise empty their bookable week.
    block_slots_from_calendar = models.BooleanField(
        default=True,
        verbose_name=_("Hide times when I am busy in Google"),
        help_text=_(
            "Only free/busy is read — bcTracker never sees the titles or the "
            "people in your other appointments."
        ),
    )

    storage_key = models.UUIDField(default=uuid4, unique=True, editable=False)
    wrapped_dek = models.BinaryField(editable=False)
    dek_nonce = models.BinaryField(editable=False)
    refresh_token_sealed = models.BinaryField(editable=False)
    # Cached so a sync tick every 15 minutes does not spend a token exchange each
    # time. Sealed under the same DEK: it is a bearer credential for an hour, and
    # an hour of read-write access to a counselor's calendar is not a secret to
    # leave in a plain column.
    access_token_sealed = models.BinaryField(blank=True, null=True, editable=False)
    access_token_expires_at = models.DateTimeField(null=True, blank=True)

    last_synced_at = models.DateTimeField(null=True, blank=True)
    # The last failure, in words a counselor can act on. Cleared by a good sync,
    # so a stale message cannot leave them chasing a problem that has gone.
    last_error = models.CharField(max_length=300, blank=True)
    # Set when Google refuses the refresh token — the counselor revoked access,
    # changed their password, or the grant expired. A revoked row is kept rather
    # than deleted so the settings page can say "reconnect" instead of silently
    # showing an unconnected state the counselor thought they had dealt with.
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager.from_queryset(GoogleCredentialQuerySet)()

    class Meta:
        verbose_name = _("Google calendar connection")

    def __str__(self) -> str:
        return f"{self.counselor} → {self.google_email}"

    @property
    def is_usable(self) -> bool:
        return self.revoked_at is None and bool(bytes(self.refresh_token_sealed or b""))

    def scope_list(self) -> list[str]:
        return self.granted_scopes.split()
