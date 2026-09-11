"""
Scheduling views.

Every route resolves its object through ``for_actor`` and then checks a
``scheduling.*`` permission, the same two-layer pattern as documents. The status
codes follow the same doctrine, and it is worth restating because scheduling is
where the two roles diverge:

  * a route keyed by a **case** — booking, the case diary's booking button —
    returns 403 to an actor who can see the case but may not act on it. The case's
    existence is not the secret.
  * a route keyed by a **booking** returns 404 when the row is not in
    ``for_actor``. On a family case, an individual appointment belonging to one
    member must not be confirmable-as-existing by another, and a 403 would confirm
    it.

``financial_admin`` is handled differently here than in documents, on purpose.
Billing may read an appointment — that is what gets invoiced — but not the notes
attached to it, and not the ministry-wide diary. See apps/scheduling/rules.py.
"""

import logging
from datetime import date, datetime, timedelta

from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import Role
from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.counseling.models import Case
from apps.scheduling import services, slots
from apps.scheduling.forms import (
    AvailabilityOverrideForm,
    AvailabilityRuleForm,
    BookingRequestForm,
    CancellationForm,
    CounselorBookingForm,
    CounselorNoteForm,
    SessionOutcomeForm,
)
from apps.scheduling.models import (
    AvailabilityOverride,
    AvailabilityRule,
    Booking,
    BookingStatus,
    GoogleCredential,
)

logger = logging.getLogger(__name__)


def visible_case_or_404(request, pk):
    return get_object_or_404(Case.objects.for_actor(request.user), pk=pk)


def visible_booking_or_404(request, pk):
    """The single door onto a Booking.

    ``for_actor`` is what makes "one member of a family case cannot see another's
    individual appointment" true by construction rather than by review.
    """
    return get_object_or_404(
        Booking.objects.for_actor(request.user).select_related(
            "case", "counselor", "counselee", "cancelled_by"
        ),
        pk=pk,
    )


def require_perm(request, perm, obj=None):
    if not request.user.has_perm(perm, obj):
        record(
            AuditVerb.ACCESS_DENIED,
            actor=request.user,
            target=obj,
            request=request,
            permission=perm,
        )
        raise PermissionDenied
    return True


def own_availability_or_404(request, model, pk):
    """A counselor's own office-hours row.

    Scoped *and* filtered on the counselor, which is belt and braces: the queryset
    lets a counselee read the rules of a counselor they are booking with, so
    ``for_actor`` alone would let one of them post a delete.
    """
    return get_object_or_404(model.objects.for_actor(request.user), pk=pk, counselor=request.user)


def _parse_date(raw, fallback=None):
    """A date from a query string, or the fallback. Never an error.

    A malformed ``?from=`` is a stale bookmark or a truncated link, and showing
    somebody a validation error on a calendar page they can simply scroll is worse
    than quietly showing them this week.
    """
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return fallback


# --- office hours ---------------------------------------------------------


@login_required
def availability(request):
    """A counselor's own office hours, and the exceptions to them.

    There is no counselor id in the URL, so this view cannot be pointed at someone
    else's diary — the same construction as the practice-settings page.
    """
    require_perm(request, "scheduling.manage_own_availability")

    today = timezone.localdate()
    return render(
        request,
        "scheduling/availability.html",
        {
            "rules": AvailabilityRule.objects.filter(counselor=request.user),
            # Past exceptions are history; a list that accumulates every holiday
            # the ministry has ever taken is a list nobody reads.
            "overrides": AvailabilityOverride.objects.filter(
                counselor=request.user, date__gte=today
            ),
            "window": services.BookingWindow(request.user),
            # Mentioned here because a Google connection changes what this page
            # means: it decides which of these hours are actually offered.
            "google_available": django_settings.GOOGLE_CALENDAR_ENABLED,
            "google": GoogleCredential.objects.filter(
                counselor=request.user, revoked_at__isnull=True
            ).first(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def availability_add(request):
    require_perm(request, "scheduling.manage_own_availability")

    form = AvailabilityRuleForm(request.POST or None, counselor=request.user)
    if request.method == "POST" and form.is_valid():
        rule = form.save()
        record(
            AuditVerb.AVAILABILITY_ADDED,
            actor=request.user,
            target=rule,
            request=request,
            weekday=rule.weekday,
            window=f"{rule.start_time}–{rule.end_time}",
        )
        messages.success(request, _("Office hours added."))
        return redirect("scheduling:availability")

    return render(
        request,
        "scheduling/availability_form.html",
        {"form": form, "rule": None, "heading": _("Add office hours")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def availability_edit(request, pk):
    rule = own_availability_or_404(request, AvailabilityRule, pk)
    require_perm(request, "scheduling.change_availabilityrule", rule)

    form = AvailabilityRuleForm(request.POST or None, instance=rule, counselor=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        record(
            AuditVerb.AVAILABILITY_UPDATED,
            actor=request.user,
            target=rule,
            request=request,
            fields=sorted(form.changed_data),
        )
        messages.success(request, _("Office hours updated."))
        return redirect("scheduling:availability")

    return render(
        request,
        "scheduling/availability_form.html",
        {"form": form, "rule": rule, "heading": _("Edit office hours")},
    )


@login_required
@require_POST
def availability_delete(request, pk):
    """Remove a weekly window.

    A hard delete, unlike almost everything else in this application. Office hours
    are a statement of intent rather than a record of what happened: appointments
    already booked into a window keep their own rows, so deleting the rule changes
    what is offered tomorrow and rewrites no history.
    """
    rule = own_availability_or_404(request, AvailabilityRule, pk)
    require_perm(request, "scheduling.delete_availabilityrule", rule)

    record(
        AuditVerb.AVAILABILITY_REMOVED,
        actor=request.user,
        target=rule,
        request=request,
        weekday=rule.weekday,
        window=f"{rule.start_time}–{rule.end_time}",
    )
    rule.delete()
    messages.success(request, _("Office hours removed. Appointments already booked still stand."))
    return redirect("scheduling:availability")


@login_required
@require_http_methods(["GET", "POST"])
def override_add(request):
    require_perm(request, "scheduling.manage_own_availability")

    form = AvailabilityOverrideForm(request.POST or None, counselor=request.user)
    if request.method == "POST" and form.is_valid():
        override = form.save()
        record(
            AuditVerb.AVAILABILITY_OVERRIDDEN,
            actor=request.user,
            target=override,
            request=request,
            date=str(override.date),
            opens_time=override.is_available,
            all_day=override.is_all_day,
            # Not the reason itself: "hospital" is nobody else's business, and
            # a counselee's name would be a disclosure.
        )
        messages.success(request, _("Calendar exception saved."))
        return redirect("scheduling:availability")

    return render(request, "scheduling/override_form.html", {"form": form})


@login_required
@require_POST
def override_delete(request, pk):
    override = own_availability_or_404(request, AvailabilityOverride, pk)
    require_perm(request, "scheduling.manage_own_availability")

    record(
        AuditVerb.AVAILABILITY_OVERRIDE_REMOVED,
        actor=request.user,
        target=override,
        request=request,
        date=str(override.date),
    )
    override.delete()
    messages.success(request, _("Calendar exception removed."))
    return redirect("scheduling:availability")


# --- booking --------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def book(request, case_pk):
    """A counselee picking one of the times their counselor is offering.

    Two steps, both server-rendered: pick a time from the list, then confirm it
    with an optional note. Two GETs rather than a grid of forms because there is no
    JavaScript here at all, and a page carrying one form per slot would repeat the
    note field forty times over.

    The times are generated on every render, including the one after a failed
    confirmation, so a slot taken while the page sat open is simply not there the
    second time. That is the courteous half of the double-booking defence; the
    exclusion constraint is the half that is actually reliable.
    """
    case = visible_case_or_404(request, case_pk)
    require_perm(request, "scheduling.add_booking", case)

    # Only a counselee books themselves in. A counselor reaching this page is
    # looking at what their counselee would see, so the busy list must not be
    # narrowed by their own diary — that is what `schedule` is for.
    booking_for = request.user if request.user.role == Role.COUNSELEE else None
    if request.method == "POST" and booking_for is None:
        # Refused here rather than after the form validates, so that a counselor
        # cannot be handed a validation error for an action that was never theirs
        # to take. They cannot be their own counselee; their route is `schedule`.
        raise PermissionDenied

    now = timezone.now()
    window = services.BookingWindow(case.counselor)
    start_date = _parse_date(request.GET.get("from"), now.astimezone(window.zone).date())
    end_date = _parse_date(request.GET.get("to"), start_date + timedelta(days=13))

    chosen = _parse_slot(request.GET.get("slot") or request.POST.get("slot"))
    form = None

    if chosen is not None:
        form = BookingRequestForm(
            request.POST if request.method == "POST" else None,
            case=case,
            initial={"slot": chosen.isoformat()},
        )
        if request.method == "POST" and form.is_valid():
            try:
                booking = services.book(
                    case=case,
                    counselee=booking_for,
                    start=form.cleaned_data["slot"],
                    minutes=window.session_minutes,
                    attendance=form.attendance_value,
                    request_note=form.cleaned_data["request_note"],
                    created_by=request.user,
                    request=request,
                )
            except services.SchedulingError as exc:
                form.add_error(None, str(exc))
                chosen = None
            else:
                messages.success(
                    request,
                    _("Requested. Your counselor will confirm, and you will get an email."),
                )
                return redirect("scheduling:detail", pk=booking.pk)

    available = services.bookable_slots(
        counselor=case.counselor,
        counselee=booking_for,
        now=now,
        start_date=start_date,
        end_date=end_date,
    )
    # Grouped in the *viewer's* zone: a counselee in another state should see
    # their own Tuesday, not their counselor's.
    return render(
        request,
        "scheduling/book.html",
        {
            "case": case,
            "form": form,
            "chosen": chosen,
            "may_book": booking_for is not None,
            "days": slots.group_by_day(available, request.user.zoneinfo),
            "window": window,
            "start_date": start_date,
            "end_date": end_date,
            "previous_from": start_date - timedelta(days=14),
            "next_from": end_date + timedelta(days=1),
            # Named so a counselee in another state can tell that 10am is their own
            # ten o'clock rather than their counselor's.
            "viewer_zone": str(request.user.zoneinfo),
        },
    )


def _parse_slot(raw):
    """An offered slot's start, from a query string or a hidden field.

    Returns None for anything unparseable rather than erroring: the value only
    decides which half of the page renders, and ``services.book`` is what checks
    that the time is actually on offer.
    """
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


@login_required
@require_http_methods(["GET", "POST"])
def schedule(request, case_pk):
    """A counselor putting an appointment in the diary directly.

    Not restricted to the published office hours — an urgent session on a Saturday
    is a real thing, and the hours exist to tell counselees what to ask for rather
    than to overrule the counselor. The clash constraint still applies.
    """
    case = visible_case_or_404(request, case_pk)
    require_perm(request, "counseling.change_case", case)
    require_perm(request, "scheduling.add_booking", case)

    window = services.BookingWindow(case.counselor)
    form = CounselorBookingForm(
        request.POST or None, case=case, default_minutes=window.session_minutes
    )

    if request.method == "POST" and form.is_valid():
        try:
            booking = services.book(
                case=case,
                counselee=form.cleaned_data["counselee"],
                start=form.cleaned_data["start"],
                minutes=form.cleaned_data["minutes"],
                attendance=form.cleaned_data["attendance"],
                created_by=request.user,
                enforce_availability=False,
                request=request,
            )
        except services.SchedulingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Booked. Everyone attending has been emailed."))
            return redirect("scheduling:detail", pk=booking.pk)

    return render(request, "scheduling/schedule.html", {"case": case, "form": form})


@login_required
def appointments(request):
    """One diary page, whichever side of the appointment the viewer is on.

    Not branched by role: the scoping layer decides what is in it, so a counselor
    sees their caseload's appointments and a counselee sees their own without this
    view knowing which. ``financial_admin`` is refused by the permission rather
    than shown an empty page — a ministry-wide diary is a caseload-shaped
    disclosure billing does not need, and it gets what it does need per case.
    """
    require_perm(request, "scheduling.view_diary")

    diary = Booking.objects.for_actor(request.user).select_related("case", "counselor", "counselee")

    # The cases this viewer may book themselves into, so the diary can offer the
    # way in. Counselees only: a counselor's route to the same slots is
    # ``schedule``, which books somebody else in and is reached from the case.
    # ``for_actor`` already excludes a membership that has ended, and ``active``
    # excludes a case that is closed or on hold.
    bookable = ()
    if request.user.role == Role.COUNSELEE:
        bookable = Case.objects.for_actor(request.user).active().select_related("counselor")

    return render(
        request,
        "scheduling/appointments.html",
        {
            "upcoming": diary.active().upcoming().order_by("slot"),
            "past": diary.past().order_by("-slot")[:50],
            "is_counselor": request.user.role == Role.COUNSELOR,
            "bookable": bookable,
        },
    )


@login_required
def case_appointments(request, case_pk):
    """Every appointment on one case that this actor may see.

    The route billing uses, which is why it exists separately from the diary: a
    financial administrator working an invoice needs the sessions on one case, and
    nothing here renders a note.
    """
    case = visible_case_or_404(request, case_pk)
    require_perm(request, "counseling.view_case", case)

    diary = (
        Booking.objects.for_actor(request.user)
        .filter(case=case)
        .select_related("counselee", "counselor")
    )
    return render(
        request,
        "scheduling/case_appointments.html",
        {
            "case": case,
            "upcoming": diary.active().upcoming().order_by("slot"),
            "past": diary.past().order_by("-slot"),
            "can_schedule": request.user.has_perm("counseling.change_case", case),
            "can_book": request.user.has_perm("scheduling.add_booking", case)
            and request.user.role == Role.COUNSELEE,
        },
    )


@login_required
def detail(request, pk):
    booking = visible_booking_or_404(request, pk)
    require_perm(request, "scheduling.view_booking", booking)

    # Decided here rather than in the template, so a template change cannot widen
    # who reads a session note.
    show_notes = request.user.has_perm("scheduling.view_booking_note", booking)

    return render(
        request,
        "scheduling/detail.html",
        {
            "booking": booking,
            "show_notes": show_notes,
            "can_confirm": request.user.has_perm("scheduling.confirm_booking", booking),
            "can_cancel": request.user.has_perm("scheduling.cancel_booking", booking),
            "can_reschedule": request.user.has_perm("scheduling.reschedule_booking", booking),
            "can_record_outcome": request.user.has_perm("scheduling.record_outcome", booking),
            # Reading a note and writing one are separate permissions: an admin may
            # read a session note but has no business authoring one.
            "can_edit_note": show_notes
            and request.user.has_perm("scheduling.change_booking_note", booking),
            "late_if_cancelled": booking.is_active
            and booking.hours_until() < services.BookingWindow(booking.counselor).notice_hours,
        },
    )


@login_required
@require_POST
def confirm(request, pk):
    booking = visible_booking_or_404(request, pk)
    require_perm(request, "scheduling.confirm_booking", booking)

    try:
        services.confirm(booking, actor=request.user, request=request)
    except services.SchedulingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Confirmed. Everyone attending has been emailed."))
    return redirect("scheduling:detail", pk=booking.pk)


@login_required
@require_http_methods(["GET", "POST"])
def cancel(request, pk):
    """Cancelling, with the notice consequence stated before it is done.

    A confirmation step rather than a bare POST button, because on a joint
    appointment cancelling affects other people and because a late cancellation may
    be billable. Being told that afterwards would be a surprise.
    """
    booking = visible_booking_or_404(request, pk)
    require_perm(request, "scheduling.cancel_booking", booking)

    window = services.BookingWindow(booking.counselor)
    would_be_late = booking.hours_until() < window.notice_hours

    form = CancellationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.cancel(
                booking,
                actor=request.user,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except services.SchedulingError as exc:
            messages.error(request, str(exc))
            return redirect("scheduling:detail", pk=booking.pk)
        messages.success(request, _("Cancelled. That time is free again."))
        return redirect("scheduling:appointments")

    return render(
        request,
        "scheduling/cancel.html",
        {
            "booking": booking,
            "form": form,
            "would_be_late": would_be_late,
            "notice_hours": window.notice_hours,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def reschedule(request, pk):
    """Move an appointment. Counselor and admin only.

    A counselee cancels and books again instead, which puts them back through the
    office hours; rescheduling bypasses them by design, which is why it is not
    theirs to do.
    """
    booking = visible_booking_or_404(request, pk)
    require_perm(request, "scheduling.reschedule_booking", booking)

    window = services.BookingWindow(booking.counselor)
    local = booking.starts_at.astimezone(window.zone)
    form = CounselorBookingForm(
        request.POST or None,
        case=booking.case,
        default_minutes=booking.duration_minutes,
        initial={
            "counselee": booking.counselee_id,
            "date": local.date(),
            "time": local.time(),
            "minutes": booking.duration_minutes,
            "attendance": booking.attendance,
        },
    )
    # Who is attending is not what rescheduling changes, and offering the field
    # here would let a move quietly become a reassignment.
    del form.fields["counselee"]
    del form.fields["attendance"]

    if request.method == "POST" and form.is_valid():
        try:
            services.reschedule(
                booking,
                actor=request.user,
                start=form.cleaned_data["start"],
                minutes=form.cleaned_data["minutes"],
                request=request,
            )
        except services.SchedulingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Moved. Everyone attending has been emailed."))
            return redirect("scheduling:detail", pk=booking.pk)

    return render(request, "scheduling/reschedule.html", {"booking": booking, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def outcome(request, pk):
    """Record what became of a session that has already happened.

    Held, or missed. This is the row a v3 invoice is raised from, which is why it
    is a deliberate action rather than something inferred from the clock: an
    appointment nobody closed out should look unfinished, not silently billable.
    """
    booking = visible_booking_or_404(request, pk)
    require_perm(request, "scheduling.record_outcome", booking)

    initial = {}
    if booking.status in (BookingStatus.COMPLETED, BookingStatus.NO_SHOW):
        initial["outcome"] = booking.status
    form = SessionOutcomeForm(
        request.POST or None,
        initial={**initial, "counselor_note": booking.counselor_note},
    )

    if request.method == "POST" and form.is_valid():
        action = (
            services.mark_completed
            if form.cleaned_data["outcome"] == "completed"
            else services.mark_no_show
        )
        try:
            action(
                booking,
                actor=request.user,
                note=form.cleaned_data["counselor_note"],
                request=request,
            )
        except services.SchedulingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Recorded."))
            return redirect("scheduling:detail", pk=booking.pk)

    return render(request, "scheduling/outcome.html", {"booking": booking, "form": form})


@login_required
@require_http_methods(["GET", "POST"])
def note(request, pk):
    """The counselor's own note on an appointment, before or after it happens.

    ``outcome`` also writes this field, but only for a session that has already
    happened and only alongside a held/missed decision. Preparing for Thursday is
    the other half of the same habit — "follow up on last week's homework" is
    written on Tuesday — and amending a note weeks later should not require
    re-answering whether the session took place.

    Counselor only, by ``scheduling.change_booking_note``. An admin can read a note
    through ``view_booking_note`` and cannot reach this route, which is the intended
    asymmetry: correcting the billing record is an administrative act, writing what
    was said in a room you were not in is not.
    """
    booking = visible_booking_or_404(request, pk)
    require_perm(request, "scheduling.change_booking_note", booking)

    # Bound to the instance for validation, but never ``form.save()``: the service
    # writes the field and the audit row together, and a form save would leave the
    # note changed with nothing in the trail saying who changed it.
    form = CounselorNoteForm(request.POST or None, instance=booking)

    if request.method == "POST" and form.is_valid():
        services.set_counselor_note(
            booking,
            actor=request.user,
            note=form.cleaned_data["counselor_note"],
            request=request,
        )
        messages.success(request, _("Note saved."))
        return redirect("scheduling:detail", pk=booking.pk)

    return render(request, "scheduling/note.html", {"booking": booking, "form": form})
