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
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import Role
from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.counseling.forms import (
    CaseCounselorForm,
    CaseForm,
    CaseMemberForm,
    CounseleeCreateForm,
    CounseleeProfileForm,
    CounselorProfileForm,
)
from apps.counseling.models import (
    Case,
    CaseMember,
    CaseStatus,
    CounseleeProfile,
    CounselorProfile,
)

logger = logging.getLogger(__name__)


def visible_case_or_404(request, pk):
    """Fetch a case the actor is allowed to see, or 404.

    The single door onto a Case in this module. Going through ``for_actor``
    rather than ``Case.objects`` is what makes "a counselor cannot reach another
    counselor's case" true by construction instead of by review.
    """
    return get_object_or_404(Case.objects.for_actor(request.user), pk=pk)


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
def case_detail(request, pk):
    case = visible_case_or_404(request, pk)
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
    require_perm(request, "counseling.add_case")

    form = CaseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        case = form.save()
        record(
            AuditVerb.CASE_CREATED,
            actor=request.user,
            target=case,
            request=request,
            counselor_id=case.counselor_id,
            kind=case.kind,
        )
        messages.success(request, _("Case opened. Add the counselees next."))
        return redirect("counseling:case_detail", pk=case.pk)

    return render(request, "counseling/case_form.html", {"form": form, "case": None})


@login_required
@require_http_methods(["GET", "POST"])
def case_edit(request, pk):
    case = visible_case_or_404(request, pk)
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
        return redirect("counseling:case_detail", pk=case.pk)

    return render(request, "counseling/case_form.html", {"form": form, "case": case})


@login_required
@require_POST
def case_close(request, pk):
    case = visible_case_or_404(request, pk)
    require_perm(request, "counseling.close_case", case)

    case.close()
    record(AuditVerb.CASE_CLOSED, actor=request.user, target=case, request=request)
    messages.success(request, _("Case closed. Its history stays available."))
    return redirect("counseling:case_detail", pk=case.pk)


# --- membership -----------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def case_member_add(request, pk):
    """Put an existing counselee on a case.

    This is the action with the real disclosure consequence in this app: after it
    runs, someone can see a case they could not see a moment ago. Administrator
    only, and audited with both parties named.
    """
    case = visible_case_or_404(request, pk)
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
        return redirect("counseling:case_detail", pk=case.pk)

    return render(request, "counseling/case_member_form.html", {"form": form, "case": case})


@login_required
@require_POST
def case_member_end(request, pk, member_pk):
    """End a membership rather than delete it.

    The dates are the record of who was in the room, and appointment and billing
    history point at them.
    """
    case = visible_case_or_404(request, pk)
    require_perm(request, "counseling.manage_case_members", case)

    member = get_object_or_404(case.members, pk=member_pk, ended_on__isnull=True)
    member.end()
    record(
        AuditVerb.CASE_MEMBER_ENDED,
        actor=request.user,
        target=case,
        request=request,
        counselee_id=str(member.counselee_id),
    )
    messages.success(request, _("Membership ended."))
    return redirect("counseling:case_detail", pk=case.pk)


# --- people ---------------------------------------------------------------


@login_required
@require_http_methods(["GET", "POST"])
def counselee_create(request):
    """Create a counselee's account and invite them in one step."""
    require_perm(request, "accounts.manage_users")

    form = CounseleeCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save(created_by=request.user, request=request)
        messages.success(
            request,
            _("Account created for %(name)s. An invitation is on its way to %(email)s.")
            % {"name": user.full_name, "email": user.email},
        )
        next_case = request.GET.get("case")
        if next_case:
            return redirect(f"{reverse('counseling:case_member_add', args=[next_case])}")
        return redirect("counseling:case_list")

    return render(request, "counseling/counselee_form.html", {"form": form})


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
def counselee_profile_edit(request, pk=None):
    """Intake details.

    A counselee maintains their own; a counselor or admin may correct one for a
    counselee on their case. Resolved through ``for_actor``, so a counselor
    reaching for someone else's profile gets 404.
    """
    require_perm(request, "counseling.change_counselee_profile")

    if pk is None:
        if request.user.role != Role.COUNSELEE:
            raise PermissionDenied
        profile, _created = CounseleeProfile.objects.get_or_create(user=request.user)
    else:
        if request.user.role == Role.COUNSELEE:
            # A counselee has exactly one profile and reaches it without an id.
            # Accepting one here would be an invitation to try someone else's.
            raise PermissionDenied
        profile = get_object_or_404(CounseleeProfile.objects.for_actor(request.user), pk=pk)

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
