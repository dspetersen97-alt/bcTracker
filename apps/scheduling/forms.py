"""
Forms for office hours and booking.

The booking form is the one worth reading. It takes an ISO timestamp rather than
a date and a time picker, because the times on offer are computed server-side and
letting the browser assemble one would mean accepting an arbitrary instant and
then arguing with it. The field is a hidden input filled in by clicking a slot;
``services.book`` still re-derives the offered list and refuses anything that is
not on it, so a hand-crafted POST gains nothing.
"""

from datetime import datetime, timedelta

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.scheduling.models import (
    MAX_SERIES_SESSIONS,
    Attendance,
    AvailabilityOverride,
    AvailabilityRule,
    Booking,
    GoogleCredential,
    Weekday,
    validate_meeting_link,
)


def MeetingLinkField(**kwargs):  # noqa: N802 — a field factory, named like the field it makes
    """The meeting-link field, defined once for the three forms that offer it.

    ``assume_scheme="https"`` so that pasting ``meet.example.com/abc-defg`` — which
    is what a person copies out of a chat message — is completed rather than
    rejected. Django 6 makes https the default; naming it now means this file does
    not change when that lands, and means the completion is not silently http on
    the way there.

    ``validate_meeting_link`` is the model's own validator rather than a second
    copy of the rule, so the form and the row cannot come to disagree about what a
    meeting link is.
    """
    return forms.URLField(
        required=False,
        max_length=500,
        assume_scheme="https",
        validators=[validate_meeting_link],
        label=_("Meeting link"),
        help_text=_("Optional. Paste the link from Zoom, Meet or Teams to make this virtual."),
        **kwargs,
    )


class AvailabilityRuleForm(forms.ModelForm):
    """A weekly window of office hours."""

    class Meta:
        model = AvailabilityRule
        fields = [
            "weekday",
            "start_time",
            "end_time",
            "slot_minutes",
            "effective_from",
            "effective_to",
        ]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "effective_to": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {
            "effective_from": _("In force from"),
            "effective_to": _("In force until"),
            "slot_minutes": _("Appointment length (minutes)"),
        }

    def __init__(self, *args, counselor, **kwargs):
        super().__init__(*args, **kwargs)
        # Not a form field. The counselor comes from the session, so there is no
        # input a request could use to write into somebody else's diary.
        self.counselor = counselor
        self.fields["weekday"].choices = Weekday.choices

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.counselor = self.counselor
        if commit:
            rule.save()
        return rule


class AvailabilityOverrideForm(forms.ModelForm):
    """A closure, or an extra window on one date."""

    class Meta:
        model = AvailabilityOverride
        fields = ["date", "is_available", "start_time", "end_time", "reason"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
        }
        labels = {"is_available": _("This opens extra time rather than closing time")}
        help_texts = {
            "start_time": _("Leave both times blank to close the whole day."),
            "reason": _("For your own reference. Counselees never see this."),
        }

    def __init__(self, *args, counselor, **kwargs):
        super().__init__(*args, **kwargs)
        self.counselor = counselor

    def save(self, commit=True):
        override = super().save(commit=False)
        override.counselor = self.counselor
        if commit:
            override.save()
        return override


class BookingRequestForm(forms.Form):
    """What a counselee sends when they pick a time.

    ``slot`` is the exact start of one of the offered times, as an ISO string. It
    is validated into an aware datetime here and checked against the offered list
    in ``services.book`` — this form's job is only to establish that the value is a
    timestamp at all.
    """

    slot = forms.CharField(widget=forms.HiddenInput)
    attendance = forms.ChoiceField(
        choices=Attendance.choices,
        initial=Attendance.INDIVIDUAL,
        label=_("Who is coming"),
        widget=forms.RadioSelect,
    )
    request_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=2000,
        label=_("Anything your counselor should know"),
        help_text=_("Optional. This is stored securely and only your counselor can read it."),
    )

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, **kwargs)
        # An individual case has nobody else to attend, so offering the choice
        # would be a question with one answer.
        if case is not None and case.members.filter(ended_on__isnull=True).count() < 2:
            del self.fields["attendance"]

    def clean_slot(self):
        raw = self.cleaned_data["slot"]
        try:
            when = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise forms.ValidationError(_("Please choose one of the times offered.")) from exc
        if when.tzinfo is None:
            # Every offered value carries an offset. A naive one means the value
            # did not come from the page, and guessing a zone for it would be how
            # an appointment lands an hour out.
            raise forms.ValidationError(_("Please choose one of the times offered."))
        return when

    @property
    def attendance_value(self):
        return self.cleaned_data.get("attendance") or Attendance.INDIVIDUAL


class BookingDetailsForm(forms.Form):
    """Everything a counselor says about an appointment except *when* it is.

    Split out because "when" is the only thing that differs between booking one
    appointment and booking a weekly series, and the two forms disagreeing about
    who may be booked, or about how a meeting link is validated, would be a bug
    nobody would find by reading either one.

    Field order is set on the subclasses rather than left to declaration order:
    inherited fields come first in Django's ordering, which would put the date
    after the length. See ``field_order``.
    """

    counselee = forms.ModelChoiceField(queryset=None, label=_("Who is this for"))
    time = forms.TimeField(widget=forms.TimeInput(attrs={"type": "time"}))
    minutes = forms.IntegerField(min_value=15, max_value=480, label=_("Length (minutes)"))
    attendance = forms.ChoiceField(
        choices=Attendance.choices,
        initial=Attendance.INDIVIDUAL,
        label=_("Who is coming"),
    )
    meeting_url = MeetingLinkField()

    def __init__(self, *args, case, default_minutes=60, **kwargs):
        super().__init__(*args, **kwargs)
        self.case = case
        self.fields["minutes"].initial = default_minutes
        # Only current members. Booking an ended membership would create an
        # appointment the model's own clean() then refuses, which is a confusing
        # way to learn that somebody left the case.
        self.fields["counselee"].queryset = case.counselees.order_by("last_name", "first_name")
        self.fields["counselee"].label_from_instance = lambda user: user.full_name

    @property
    def counselor_zone(self):
        """The zone a typed time means. Always the counselor's.

        They typed "Tuesday at 2" while looking at their own week, so their own two
        o'clock is what they meant — even when the counselee is in another state and
        will be shown the same appointment as eleven in the morning.
        """
        return self.case.counselor.zoneinfo


class CounselorBookingForm(BookingDetailsForm):
    """What a counselor sends when scheduling one appointment on someone's behalf.

    A real date and time rather than a slot to click: a counselor fitting in an
    urgent session is not choosing from the published hours, so there is no list to
    pick from. ``services.book`` is called with ``enforce_availability=False``, and
    the exclusion constraint is still the thing that stops a clash.
    """

    date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))

    field_order = ["counselee", "date", "time", "minutes", "attendance", "meeting_url"]

    def clean(self):
        cleaned = super().clean()
        day, moment = cleaned.get("date"), cleaned.get("time")
        if day and moment:
            cleaned["start"] = datetime.combine(day, moment).replace(tzinfo=self.counselor_zone)
        return cleaned


class RecurringBookingForm(BookingDetailsForm):
    """What a counselor sends to put a weekly series in the diary.

    A weekday instead of a date, because that is the shape of the arrangement being
    made: "Tuesdays at two, for the next ten weeks" is one decision, and asking for
    ten dates would be asking the counselor to do the arithmetic — including the
    week the clocks change.

    The first session is worked out here rather than typed: the soonest occurrence
    of the chosen weekday whose start time has not already passed. A form that
    accepted a start date as well would let a counselor book a series into last
    month, and ``services.book_series`` walks forward from whatever it is given.
    """

    weekday = forms.TypedChoiceField(
        choices=Weekday.choices,
        coerce=int,
        label=_("Day of the week"),
        help_text=_("The first session is the next one of these that has not passed."),
    )
    occurrences = forms.IntegerField(
        min_value=2,
        max_value=MAX_SERIES_SESSIONS,
        initial=8,
        label=_("How many sessions?"),
        help_text=_("Booked weekly from the first one."),
    )

    field_order = [
        "counselee",
        "weekday",
        "time",
        "minutes",
        "occurrences",
        "attendance",
        "meeting_url",
    ]

    def clean(self):
        cleaned = super().clean()
        weekday, moment = cleaned.get("weekday"), cleaned.get("time")
        if weekday is not None and moment:
            cleaned["start"] = self._first_occurrence(weekday, moment)
        return cleaned

    def _first_occurrence(self, weekday: int, moment):
        """The next ``weekday`` at ``moment``, in the counselor's own zone.

        ``timezone.now()`` rather than a passed-in clock, and the comparison is
        against the instant rather than the date: a counselor arranging Tuesdays at
        two on a Tuesday morning means *today*, and one doing it on Tuesday evening
        does not.
        """
        now = timezone.now()
        today = now.astimezone(self.counselor_zone).date()
        candidate = datetime.combine(
            today + timedelta(days=(weekday - today.weekday()) % 7), moment
        ).replace(tzinfo=self.counselor_zone)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate


class MeetingLinkForm(forms.Form):
    """Adding, changing or clearing the link for a session — or for the rest of a series.

    Separate from the booking form because a counselor who was sent the link after
    arranging the sessions is the ordinary case, not the exception. Clearing it is
    the same action as setting it, which is why the field is not required.

    ``apply_to_series`` is removed rather than merely unticked when the appointment
    is not part of a series, so the page never asks a question with one answer.
    """

    meeting_url = MeetingLinkField()
    apply_to_series = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Use this for the rest of the series too"),
        help_text=_("Sessions that have already happened are left alone."),
    )

    def __init__(self, *args, in_a_series=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not in_a_series:
            del self.fields["apply_to_series"]

    @property
    def applies_to_series(self) -> bool:
        return bool(self.cleaned_data.get("apply_to_series"))


class CancellationForm(forms.Form):
    reason = forms.CharField(
        required=False,
        max_length=200,
        label=_("Reason (optional)"),
        help_text=_("Your counselor will see this."),
    )


class SessionOutcomeForm(forms.Form):
    """What the counselor records after a session.

    The note lives on the Booking rather than in a document because it is about
    the appointment, and because a session note that is a document would be one
    more thing to remember to keep away from billing. It is behind
    ``scheduling.view_booking_note`` instead.
    """

    OUTCOMES = [
        ("completed", _("The session was held")),
        ("no_show", _("Nobody attended")),
    ]

    outcome = forms.ChoiceField(choices=OUTCOMES, widget=forms.RadioSelect)
    counselor_note = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5}),
        label=_("Session note"),
        help_text=_("Only you and an administrator can read this. The counselee cannot."),
    )


class CounselorNoteForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = ["counselor_note"]
        widgets = {"counselor_note": forms.Textarea(attrs={"rows": 6})}
        labels = {"counselor_note": _("Session note")}


class GoogleCalendarSettingsForm(forms.ModelForm):
    """The three choices a counselor gets over their own connection.

    ``calendar_id`` is a free-text field rather than a dropdown of the counselor's
    calendars, which would need a fourth API scope — ``calendar.calendarlist`` —
    to populate. Asking for read access to the list of every calendar a counselor
    subscribes to, in order to save them pasting one string, is a bad trade.
    """

    class Meta:
        model = GoogleCredential
        fields = ["calendar_id", "include_names", "block_slots_from_calendar"]
        labels = {"calendar_id": _("Calendar")}
        help_texts = {
            "calendar_id": _(
                "Leave as “primary” for your main calendar, or paste the ID of "
                "another one from its Google settings."
            ),
        }

    def clean_calendar_id(self):
        # Whitespace from a paste would produce a 404 from Google that reads as a
        # broken integration rather than a typo.
        return self.cleaned_data["calendar_id"].strip() or "primary"
