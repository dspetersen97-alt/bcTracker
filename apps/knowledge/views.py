"""
The knowledge base's pages.

Every one of them resolves its row through ``for_actor`` and then checks a
permission, which for this app means the same thing twice over for two of the four
roles: a counselee and a financial administrator find no rows *and* hold none of the
permissions. That is deliberate belt and braces on a shelf that carries counselors'
notes to each other — see the module docstring in models.py.

``download`` is the only route here that reaches the encrypted volume, and it works
the way documents' does: the permission check and the audit row are inside
``services.open_resource``, beside the decryption, rather than out here.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core.downloads import encrypted_file_response
from apps.documents.crypto import DecryptionError
from apps.knowledge import services
from apps.knowledge.forms import CommentForm, ResourceEditForm, ResourceForm
from apps.knowledge.models import Resource, ResourceComment

logger = logging.getLogger(__name__)


def require_perm(request, perm, obj=None):
    """As in documents' views: refuse, and record the refusal.

    Repeated rather than imported so that this app's refusals do not depend on a
    helper living in an app about something else. It is four lines, and the audit row
    is the part that matters.
    """
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


def visible_resource_or_404(request, public_id):
    """The single door onto a Resource.

    ``for_actor`` means "staff who counsel, and nobody else" here, so a counselee or a
    financial administrator gets a 404 on every one of these routes rather than a
    refusal: for them the knowledge base does not exist.
    """
    return get_object_or_404(
        Resource.objects.for_actor(request.user).select_related("contributed_by"),
        public_id=public_id,
    )


@login_required
def home(request):
    """The shelf, searchable. The page the feature was asked for.

    Counting comments here rather than in the template because a per-row query in a
    loop is how a page of forty resources becomes forty-one queries. The count is
    filtered to live comments so that removing one does not leave the shelf claiming
    a conversation that is no longer there.

    The ``order_by`` repeats what ``Resource.Meta.ordering`` already says, and it is
    not redundant: Django drops a model's default ordering from a query that groups,
    because the ordering columns would have to join the ``GROUP BY``. The count above
    makes this such a query, so without this line the shelf comes back in whatever
    order Postgres finds convenient.
    """
    require_perm(request, "knowledge.view_resources")

    term = request.GET.get("q", "")
    resources = (
        Resource.objects.for_actor(request.user)
        .matching(term)
        .select_related("contributed_by")
        .annotate(comment_count=Count("comments", filter=Q(comments__deleted_at__isnull=True)))
        .order_by("-created_at")
    )

    return render(
        request,
        "knowledge/home.html",
        {
            "resources": resources,
            "q": term,
            "can_contribute": request.user.has_perm("knowledge.add_resource"),
        },
    )


@login_required
def detail(request, public_id):
    """One resource, its comments, and the box to add another.

    The comment form is rendered for anybody who may comment, and the ``POST`` goes to
    a route of its own: a GET page that also accepts a write would have to decide what
    to do when the write fails, and the redirect after success is what stops a refresh
    posting the same note twice.
    """
    resource = visible_resource_or_404(request, public_id)
    require_perm(request, "knowledge.view_resources")

    comments = list(resource.comments.filter(deleted_at__isnull=True).select_related("author"))
    # Asked here, per comment, rather than restated in the markup: "the author, or an
    # administrator" is written once, in rules.py, and a template cannot pass an object
    # to ``has_perm``. Cheap — these are predicates over two integers, not queries.
    for row in comments:
        row.may_remove = request.user.has_perm("knowledge.delete_comment", row)

    return render(
        request,
        "knowledge/detail.html",
        {
            "resource": resource,
            "comments": comments,
            "form": CommentForm() if request.user.has_perm("knowledge.add_comment") else None,
            "can_change": request.user.has_perm("knowledge.change_resource", resource),
            "can_delete": request.user.has_perm("knowledge.delete_resource", resource),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def add(request):
    require_perm(request, "knowledge.add_resource")

    form = ResourceForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            resource = services.store_resource(
                contributed_by=request.user,
                title=form.cleaned_data["title"],
                summary=form.cleaned_data["summary"],
                topics=form.cleaned_data["topics"],
                kind=form.cleaned_data["kind"],
                url=form.cleaned_data["url"],
                upload=form.cleaned_data.get("file") or None,
                request=request,
            )
        except services.UploadRejected as exc:
            form.add_error("file", str(exc))
        else:
            messages.success(
                request,
                _("“%(title)s” is on the shelf. Every counselor can find it now.")
                % {"title": resource.title},
            )
            return redirect("knowledge:detail", public_id=resource.public_id)

    return render(request, "knowledge/add.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def edit(request, public_id):
    resource = visible_resource_or_404(request, public_id)
    require_perm(request, "knowledge.change_resource", resource)

    form = ResourceEditForm(request.POST or None, instance=resource)
    if request.method == "POST" and form.is_valid():
        form.save()
        record(
            AuditVerb.KNOWLEDGE_RESOURCE_UPDATED,
            actor=request.user,
            target=resource,
            request=request,
            fields=sorted(form.changed_data),
        )
        messages.success(request, _("Saved."))
        return redirect("knowledge:detail", public_id=resource.public_id)

    return render(request, "knowledge/edit.html", {"form": form, "resource": resource})


@login_required
@require_POST
def remove(request, public_id):
    resource = visible_resource_or_404(request, public_id)
    require_perm(request, "knowledge.delete_resource", resource)

    services.remove_resource(resource, actor=request.user, request=request)
    messages.success(request, _("Taken off the shelf."))
    return redirect("knowledge:home")


@login_required
def download(request, public_id):
    """Hand over the file on a resource — to print, or to read offline."""
    resource = visible_resource_or_404(request, public_id)

    try:
        frames = services.open_resource(resource, actor=request.user, request=request)
    except FileNotFoundError:
        # Either a link-only resource, which is an ordinary 404, or a row whose blob
        # has gone, which is not. Logged so the second case is visible; both look the
        # same to whoever clicked, because there is nothing for them to do about it.
        logger.info("No file to serve for resource %s (%s)", resource.pk, resource.storage_key)
        raise Http404 from None
    except DecryptionError:
        logger.error("Resource %s failed to decrypt", resource.pk)
        raise Http404 from None

    return encrypted_file_response(
        frames,
        filename=resource.original_filename,
        content_type=resource.content_type,
        byte_size=resource.byte_size,
    )


@login_required
@require_POST
def comment(request, public_id):
    resource = visible_resource_or_404(request, public_id)
    require_perm(request, "knowledge.add_comment")

    form = CommentForm(request.POST)
    if form.is_valid():
        services.add_comment(
            resource,
            author=request.user,
            body=form.cleaned_data["body"],
            request=request,
        )
    else:
        # An empty note is the only way this fails, and re-rendering the whole page
        # with an error on a textarea somebody left blank would be a scolding. Saying
        # nothing happened is enough.
        messages.error(request, _("A note needs something in it."))

    return redirect("knowledge:detail", public_id=resource.public_id)


@login_required
@require_POST
def comment_remove(request, public_id):
    """Take a note down. The author, or an administrator — see rules.py."""
    comment_row = get_object_or_404(
        ResourceComment.objects.for_actor(request.user).select_related("resource", "author"),
        public_id=public_id,
    )
    require_perm(request, "knowledge.delete_comment", comment_row)

    services.remove_comment(comment_row, actor=request.user, request=request)
    messages.success(request, _("Note removed."))
    return redirect("knowledge:detail", public_id=comment_row.resource.public_id)
