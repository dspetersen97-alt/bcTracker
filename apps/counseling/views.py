"""
Case management and the role dashboards.

Every view here resolves objects through ``ScopedQuerysetMixin``, so an actor
asking for a case they may not see gets 404 — not 403, which would confirm the
case exists. Mutating views additionally check a ``counseling.*`` permission,
because "may appear in my list" and "may be changed by me" are different
questions and the counselee is the clearest case of the difference: a counselee
can see their case and can change nothing about it.

The dashboards are deliberately separate views rather than one template full of
role conditionals. A conditional that gets inverted shows the wrong person the
wrong caseload; a separate view that is never routed for a role cannot.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import Role
from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core import mail as core_mail
from apps.core.http import safe_next, with_query
from apps.core.ids import looks_like_public_id
from apps.counseling.forms import (
    CaseCounselorForm,
    CaseCreateForm,
    CaseForm,
    CaseMemberForm,
    CounseleeCreateForm,
    CounseleeProfileForm,
    CounselorProfileForm,
    eligible_counselees,
)
from apps.counseling.models import (
    Case,
    CaseMember,
    CaseStatus,
    CounseleeProfile,
    CounselorProfile,
)
from apps.documents.models import Document, Visibility
from apps.scheduling.models import Attendance, Booking

logger = logging.getLogger(__name__)


def visible_case_or_404(request, public_id):
    """Fetch a case the actor is allowed to see, or 404.

    The single door onto a Case in this module. Going through ``for_actor``
    rather than ``Case.objects`` is what makes "a counselor cannot reach another
    counselor's case" true by construction instead of by review.

    Keyed by ``public_id`` rather than ``pk``, which is what every route in this app
    now carries: see ``apps/core/ids.py``. The primary key is still what foreign keys
    and audit metadata use — it just never appears in a URL.
    """
    return get_object_or_404(Case.objects.for_actor(request.user), public_id=public_id)


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


# --- dashboards -----------------------------------------------------------


@login_required
def dashboard(request):
    """Send each role to the view built for it.

    A thin router rather than a page, so ``accounts:home`` has one destination
    and the role-specific views never have to guard against the wrong audience.
    """
    match request.user.role:
        case Role.COUNSELOR:
            return redirect("counseling:counselor_dashboard")
        case Role.ADMIN:
            return redirect("counseling:case_list")
        case Role.FINANCIAL_ADMIN:
            return redirect("counseling:caseload_index")
        case _:
            return redirect("counseling:my_cases")


@login_required
def counselor_dashboard(request):
    """A counselor's own caseload.

    Uses ``for_actor`` even though the role is already known, so the page is
    correct for the wrong reason never — if the scoping rule changes, this
    follows it.
    """
    if request.user.role != Role.COUNSELOR:
        return redirect("counseling:dashboard")

    cases = (
        Case.objects.for_actor(request.user)
        .select_related("counselor")
        .prefetch_related("members__counselee")
        .annotate(member_count=Count("members", filter=Q(members__ended_on__isnull=True)))
    )
    return render(
        request,
        "counseling/counselor_dashboard.html",
        {
            "active_cases": [c for c in cases if c.status == CaseStatus.ACTIVE],
            "other_cases": [c for c in cases if c.status != CaseStatus.ACTIVE],
            "profile": CounselorProfile.objects.filter(user=request.user).first(),
        },
    )


@login_required
def my_cases(request):
    """What a counselee sees: their own case, and nothing about anyone else's."""
    if request.user.role != Role.COUNSELEE:
        return redirect("counseling:dashboard")

    cases = Case.objects.for_actor(request.user).select_related("counselor")
    return render(request, "counseling/my_cases.html", {"cases": cases})


@login_required
def caseload_index(request):
    """Which counselees each counselor carries. For billing.

    The one view financial_admin has into the counseling side, and it shows
    counts and names — never notes, never documents. The permission is named
    rather than checked as a role so the intent survives a future reorganisation.
    """
    require_perm(request, "accounts.view_caseload_index")

    counselors = (
        Case.objects.for_actor(request.user)
        .values("counselor__id", "counselor__first_name", "counselor__last_name")
        .annotate(
            case_count=Count("id", distinct=True),
            counselee_count=Count(
                "members__counselee", distinct=True, filter=Q(members__ended_on__isnull=True)
            ),
        )
        .order_by("counselor__last_name", "counselor__first_name")
    )
    return render(request, "counseling/caseload_index.html", {"rows": counselors})


# --- cases ----------------------------------------------------------------


@login_required
def case_list(request):
    """The staff case index. Every case the actor may see; admins get all of them.

    Counselees go to their own view instead. The scoping layer would already
    narrow this to their own case, but the columns are wrong for them — a member
    count on a shared case tells a counselee how many other people are on it,
    which is not theirs to learn from a table.
    """
    if request.user.role == Role.COUNSELEE:
        return redirect("counseling:my_cases")

    cases = (
        Case.objects.for_actor(request.user)
        .select_related("counselor")
        .prefetch_related("members__counselee")
        .annotate(member_count=Count("members", filter=Q(members__ended_on__isnull=True)))
    )
    status = request.GET.get("status")
    if status in CaseStatus.values:
        cases = cases.filter(status=status)
    return render(
        request,
        "counseling/case_list.html",
        {
            "cases": cases,
            "statuses": CaseStatus.choices,
            "selected_status": status,
            "can_add": request.user.has_perm("counseling.add_case"),
        },
    )


@login_required
def case_detail(request, public_id):
    case = visible_case_or_404(request, public_id)
    require_perm(request, "counseling.view_case", case)
    record(AuditVerb.CASE_VIEWED, actor=request.user, target=case, request=request)

    # financial_admin may confirm a case exists and who is on it; the presenting
    # concern is not theirs to read. Decided here rather than in the template so
    # a template change cannot widen it.
    show_notes = request.user.has_perm("counseling.change_case", case)

    # Scoped rather than case.members.all(): on a shared case a counselee sees
    # only their own membership row. Reaching the case does not entitle them to
    # the roster — see CaseMemberQuerySet.scope_for_counselee.
    members = (
        CaseMember.objects.for_actor(request.user)
        .filter(case=case)
        .select_related("counselee")
        .order_by("joined_on")
    )

    return render(
        request,
        "counseling/case_detail.html",
        {
            "case": case,
            "members": members,
            "show_notes": show_notes,
            "can_change": show_notes,
            "can_manage_members": request.user.has_perm("counseling.manage_case_members", case),
            # Whether each name on the roster becomes a link to that person's file.
            # Staff only; a financial administrator reading this page sees names, as
            # billing needs, and no way through to what the file contains.
            "can_view_counselees": request.user.has_perm("counseling.view_counselee"),
            "can_close": request.user.has_perm("counseling.close_case", case),
            # The same permission the documents list itself checks, so the link is
            # hidden from financial_admin for the reason the page would refuse
            # them — not because a template author remembered to.
            "can_see_documents": request.user.has_perm("documents.view_case_documents", case),
            # Likewise for correspondence: hidden from financial_admin because the
            # page would refuse them, and shown to an administrator because they
            # may read it — see the note at the top of apps/messaging/rules.py.
            "can_see_messages": request.user.has_perm("messaging.view_case_threads", case),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def case_create(request):
    """Open a case, and put the counselees on it in the same step.

    ``?counselee=<public id>`` pre-selects somebody, which is how the page that creates a
    counselee hands them back: an administrator who left here to take on a new
    counselee returns to find them already ticked rather than having to find them
    in a list of everybody the ministry has ever seen.

    Adding a member has a real disclosure consequence, so each one is audited
    individually and with the same verb the membership page uses — the trail
    should not depend on which page the membership was created from.
    """
    require_perm(request, "counseling.add_case")

    initial = {}
    preselected = request.GET.getlist("counselee")
    if preselected:
        initial["counselees"] = eligible_counselees().filter(public_id__in=preselected)

    form = CaseCreateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            case = form.save()
            record(
                AuditVerb.CASE_CREATED,
                actor=request.user,
                target=case,
                request=request,
                counselor_id=case.counselor_id,
                kind=case.kind,
            )
            for member in form.members:
                record(
                    AuditVerb.CASE_MEMBER_ADDED,
                    actor=request.user,
                    target=case,
                    request=request,
                    counselee_id=str(member.counselee_id),
                    counselee_email=member.counselee.email,
                )
        if form.members:
            messages.success(
                request,
                _("Case opened with %(names)s on it.")
                % {"names": ", ".join(m.counselee.full_name for m in form.members)},
            )
        else:
            messages.success(request, _("Case opened. Add the counselees next."))
        return redirect("counseling:case_detail", public_id=case.public_id)

    return render(
        request,
        "counseling/case_form.html",
        {
            "form": form,
            "case": None,
            # Where the New Counselee page should send them back to, with whatever
            # is already selected kept — see counselee_create.
            "counselee_create_url": with_query(
                reverse("counseling:counselee_create"),
                next=reverse("counseling:case_create"),
            ),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def case_edit(request, public_id):
    case = visible_case_or_404(request, public_id)
    require_perm(request, "counseling.change_case", case)

    # An administrator may reassign; the assigned counselor may not, because
    # reassignment moves who can read the case's documents.
    may_reassign = request.user.has_perm("counseling.manage_case_members", case)
    form_class = CaseForm if may_reassign else CaseCounselorForm
    previous_counselor_id = case.counselor_id

    form = form_class(request.POST or None, instance=case)
    if request.method == "POST" and form.is_valid():
        case = form.save()
        if case.counselor_id != previous_counselor_id:
            record(
                AuditVerb.CASE_REASSIGNED,
                actor=request.user,
                target=case,
                request=request,
                previous_counselor_id=previous_counselor_id,
                counselor_id=case.counselor_id,
            )
        record(
            AuditVerb.CASE_UPDATED,
            actor=request.user,
            target=case,
            request=request,
            fields=sorted(form.changed_data),
        )
        messages.success(request, _("Case updated."))
        return redirect("counseling:case_detail", public_id=case.public_id)

    return render(request, "counseling/case_form.html", {"form": form, "case": case})


@login_required
@require_POST
def case_close(request, public_id):
    case = visible_case_or_404(request, public_id)
    require_perm(request, "counseling.close_case", case)

    case.close()
    record(AuditVerb.CASE_CLOSED, actor=request.user, target=case, request=request)
    messages.success(request, _("Case closed. Its history stays available."))
    return redirect("counseling:case_detail", public_id=case.public_id)


# --- membership -----------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def case_member_add(request, public_id):
    """Put an existing counselee on a case.

    This is the action with the real disclosure consequence in this app: after it
    runs, someone can see a case they could not see a moment ago. Administrator
    only, and audited with both parties named.
    """
    case = visible_case_or_404(request, public_id)
    require_perm(request, "counseling.manage_case_members", case)

    form = CaseMemberForm(request.POST or None, case=case)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        record(
            AuditVerb.CASE_MEMBER_ADDED,
            actor=request.user,
            target=case,
            request=request,
            counselee_id=str(member.counselee_id),
            counselee_email=member.counselee.email,
        )
        messages.success(
            request,
            _("%(name)s added to the case.") % {"name": member.counselee.full_name},
        )
        return redirect("counseling:case_detail", public_id=case.public_id)

    return render(request, "counseling/case_member_form.html", {"form": form, "case": case})


@login_required
@require_POST
def case_member_end(request, public_id, member_public_id):
    """End a membership rather than delete it.

    The dates are the record of who was in the room, and appointment and billing
    history point at them.
    """
    case = visible_case_or_404(request, public_id)
    require_perm(request, "counseling.manage_case_members", case)

    member = get_object_or_404(case.members, public_id=member_public_id, ended_on__isnull=True)
    member.end()
    record(
        AuditVerb.CASE_MEMBER_ENDED,
        actor=request.user,
        target=case,
        request=request,
        counselee_id=str(member.counselee_id),
    )
    messages.success(request, _("Membership ended."))
    return redirect("counseling:case_detail", public_id=case.public_id)


# --- people ---------------------------------------------------------------


@login_required
def counselee_detail(request, public_id):
    """One counselee's file, in one place: sessions, documents, and notes.

    The counselor's request, and the reason it is worth a page of its own: before a
    session they want the last few meetings, the next one, and what has been sent
    in — which otherwise means the case page, the diary, and the documents list in
    three tabs.

    Every list on it is a ``for_actor`` queryset, and the page exists at all only
    for someone who already holds a membership row for this person:

      * ``CaseMember.objects.for_actor`` decides which of this counselee's cases
        the viewer may see, and a viewer with none gets 404 rather than an empty
        page. A counselor cannot open the file of somebody on another counselor's
        caseload, and the 404 does not confirm the account exists.
      * the sessions and the documents are then narrowed to *those* cases, so a
        counselee who is also on a case elsewhere in the ministry does not bring
        that case's history along.
      * notes are per-case behind ``counseling.change_case`` and per-session behind
        ``scheduling.view_booking_note``, which is the same pair of checks the case
        page and the appointment page make. Decided here, so a template change
        cannot widen who reads a note.

    ``financial_admin`` is refused by ``counseling.view_counselee``: this is the
    counseling record gathered up, and gathering it is exactly what makes billing's
    absence matter.
    """
    require_perm(request, "counseling.view_counselee")

    memberships = list(
        CaseMember.objects.for_actor(request.user)
        .filter(counselee__public_id=public_id)
        .select_related("case", "case__counselor", "counselee")
        .order_by("-joined_on")
    )
    if not memberships:
        raise Http404

    counselee = memberships[0].counselee
    cases = [membership.case for membership in memberships]

    # A joint appointment counts as this person's session even when the ``counselee``
    # column names whoever arranged it — on a couple's case both of them were in the
    # room, and a page that left it out would show a gap where a session was.
    sessions = (
        Booking.objects.for_actor(request.user)
        .filter(case__in=cases)
        .filter(Q(counselee=counselee) | Q(attendance=Attendance.WHOLE_CASE))
        .select_related("case", "counselor", "counselee")
    )
    past_sessions = list(sessions.past().order_by("-slot"))
    next_session = sessions.active().upcoming().order_by("slot").first()

    # What is in this person's file: what they sent in, and what the counselor put
    # in front of the whole case. Not another member's private upload — the counselor
    # may read that on the case's own documents page, where it is attributed to the
    # person who actually sent it rather than filed under this one.
    documents = (
        Document.objects.for_actor(request.user)
        .filter(case__in=cases)
        .filter(Q(owner=counselee) | Q(visibility=Visibility.CASE_SHARED))
        .select_related("owner", "case", "booking")
        .order_by("-created_at")
    )

    case_rows = [
        {
            "case": membership.case,
            "membership": membership,
            "notes": (
                membership.case.notes
                if request.user.has_perm("counseling.change_case", membership.case)
                else ""
            ),
        }
        for membership in memberships
    ]
    session_notes = [
        session
        for session in past_sessions
        if session.counselor_note and request.user.has_perm("scheduling.view_booking_note", session)
    ]

    record(
        AuditVerb.COUNSELEE_VIEWED,
        actor=request.user,
        target=counselee,
        request=request,
        case_ids=[case.pk for case in cases],
    )
    return render(
        request,
        "counseling/counselee_detail.html",
        {
            "counselee": counselee,
            "case_rows": case_rows,
            "past_sessions": past_sessions,
            "next_session": next_session,
            "documents": documents,
            "session_notes": session_notes,
            "profile": CounseleeProfile.objects.for_actor(request.user)
            .filter(user=counselee)
            .first(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def counselee_create(request):
    """Create a counselee's account and invite them in one step.

    Both ways back, because this page is only ever reached from somewhere else:

      * ``?case=`` returns to adding them to an existing case;
      * ``?next=`` returns to whatever sent them, with ``counselee=<public id>`` added so
        the New Case page can tick the person who did not exist a moment ago. A
        redirect back to a half-filled form the administrator has to fill in from
        memory is the kind of small friction that ends with the counselee being
        created and then forgotten.

    When the invitation could not be emailed the account still exists and the link
    is shown once — the same fallback as the New Person page, and for the same
    reason: an installation whose mail is not working yet must still be able to
    take on a counselee. See ``apps.accounts.services.invite_or_hand_over``.
    """
    require_perm(request, "accounts.manage_users")

    # Shape-checked before it reaches reverse(): a ``?case=`` that could not name a
    # case would raise NoReverseMatch and turn a mistyped link into a 500. Dropping it
    # falls through to the ``?next=`` branch, which is the safe half of the pair.
    next_case = request.GET.get("case", "")
    if not looks_like_public_id(next_case):
        next_case = ""

    form = CounseleeCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save(created_by=request.user, request=request)
        if next_case:
            after = reverse("counseling:case_member_add", args=[next_case])
        else:
            # ``counselee=<public id>`` only when somebody asked to be sent back. The case
            # list has no use for it, and a parameter that does nothing invites the
            # question of what it does.
            returning_to = safe_next(request, "")
            after = (
                with_query(returning_to, counselee=user.public_id)
                if returning_to
                else reverse("counseling:case_list")
            )
        if form.invitation_link:
            return render(
                request,
                "accounts/user_created.html",
                {
                    "created_user": user,
                    "invitation_link": form.invitation_link,
                    "mail_reason": core_mail.unconfigured_reason(),
                    "can_configure_mail": request.user.has_perm("core.manage_site_settings"),
                    "next_url": after,
                },
            )
        messages.success(
            request,
            _("Account created for %(name)s. An invitation is on its way to %(email)s.")
            % {"name": user.full_name, "email": user.email},
        )
        return redirect(after)

    return render(
        request,
        "counseling/counselee_form.html",
        {"form": form, "mail_reason": core_mail.unconfigured_reason(), "case_id": next_case},
    )


@login_required
@require_http_methods(["GET", "POST"])
def counselor_profile_edit(request):
    """A counselor's own practice settings.

    Scoped to ``request.user`` by construction: there is no counselor id in the
    URL, so this view cannot be pointed at somebody else's profile.
    """
    require_perm(request, "counseling.change_own_counselor_profile")

    profile, _created = CounselorProfile.objects.get_or_create(user=request.user)
    form = CounselorProfileForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        record(
            AuditVerb.PROFILE_UPDATED,
            actor=request.user,
            target=request.user,
            request=request,
            fields=sorted(form.changed_data),
        )
        messages.success(request, _("Your practice settings have been saved."))
        return redirect("counseling:counselor_dashboard")

    return render(request, "counseling/counselor_profile_form.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def counselee_profile_edit(request, public_id=None):
    """Intake details.

    A counselee maintains their own; a counselor or admin may correct one for a
    counselee on their case. Resolved through ``for_actor``, so a counselor
    reaching for someone else's profile gets 404.
    """
    require_perm(request, "counseling.change_counselee_profile")

    if public_id is None:
        if request.user.role != Role.COUNSELEE:
            raise PermissionDenied
        profile, _created = CounseleeProfile.objects.get_or_create(user=request.user)
    else:
        if request.user.role == Role.COUNSELEE:
            # A counselee has exactly one profile and reaches it without an id.
            # Accepting one here would be an invitation to try someone else's.
            raise PermissionDenied
        profile = get_object_or_404(
            CounseleeProfile.objects.for_actor(request.user), public_id=public_id
        )

    form = CounseleeProfileForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        form.save()
        record(
            AuditVerb.PROFILE_UPDATED,
            actor=request.user,
            target=profile.user,
            request=request,
            fields=sorted(form.changed_data),
        )
        messages.success(request, _("Details saved."))
        return redirect("counseling:dashboard")

    return render(
        request,
        "counseling/counselee_profile_form.html",
        {"form": form, "profile": profile, "is_own": profile.user_id == request.user.pk},
    )
