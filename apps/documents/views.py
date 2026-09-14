"""
Document views. The only route out of the encrypted store.

There is no MEDIA_URL and no static mapping onto the document volume, so every
byte a browser receives passes through ``download`` below, which resolves the
document through ``for_actor``, re-checks the permission, and writes an audit row
before the first frame is decrypted.

The response headers a download needs are not cosmetic, and they are shared with
messaging's attachments — see ``apps/core/downloads.py``, which states them once
and explains each.
"""

import logging
from itertools import chain

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core.downloads import (
    encrypted_file_response,
    inline_file_response,
    may_be_shown_inline,
)
from apps.core.ids import looks_like_public_id
from apps.counseling.models import Case
from apps.documents import services
from apps.documents.crypto import DecryptionError
from apps.documents.forms import (
    DocumentEditForm,
    DocumentTemplateEditForm,
    DocumentTemplateUploadForm,
    DocumentUploadForm,
    UseTemplateForm,
)
from apps.documents.models import Document, DocumentTemplate, Visibility
from apps.scheduling.models import Booking

logger = logging.getLogger(__name__)


def visible_case_or_404(request, public_id):
    return get_object_or_404(Case.objects.for_actor(request.user), public_id=public_id)


def visible_document_or_404(request, public_id):
    """The single door onto a Document.

    ``for_actor`` is what makes "a spouse cannot reach the other's private
    upload" true by construction: the row is not in the queryset, so this is a
    404 and the document's existence is never confirmed.

    Keyed by ``public_id``, which is what the routes carry — the primary key of a
    document is never published, so a link to one cannot be turned into a link to the
    next one by adding one. See ``apps/core/ids.py``.
    """
    return get_object_or_404(
        Document.objects.for_actor(request.user).select_related("case", "owner"),
        public_id=public_id,
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


@login_required
def case_documents(request, case_public_id):
    """Everything on a case that this actor may see.

    The template is given the scoped queryset and nothing else — no total, no
    "3 hidden" — because a count is content: it would tell a counselee that their
    spouse has sent something, which is precisely what visibility protects.
    """
    case = visible_case_or_404(request, case_public_id)
    require_perm(request, "documents.view_case_documents", case)

    documents = (
        Document.objects.for_actor(request.user)
        .filter(case=case)
        # "case" as well as "owner": the template's source label compares the
        # uploader against the case counselor, and one query per row would be a
        # silly way to render a name.
        .select_related("owner", "case")
        .order_by("-created_at")
    )
    return render(
        request,
        "documents/case_documents.html",
        {
            "case": case,
            "documents": documents,
            "can_upload": request.user.has_perm("documents.add_document", case),
            "can_share": request.user.has_perm("counseling.change_case", case),
            # The way in to the library, from the page a counselor is on when they
            # want the intake form. A counselee's page never shows it — see
            # documents.use_document_template.
            "can_use_templates": request.user.has_perm("documents.use_document_template", case),
        },
    )


@login_required
def my_documents(request):
    """A counselee's own view: what they sent, and what was shared with them.

    Also reachable by staff, where it means "everything on my caseload" — the
    scoping layer decides which, so this view does not branch on the role.
    """
    require_perm(request, "documents.view_document_index")

    documents = (
        Document.objects.for_actor(request.user)
        .select_related("case", "case__counselor", "owner")
        .order_by("-created_at")
    )
    return render(request, "documents/my_documents.html", {"documents": documents})


def session_being_uploaded_for(request, case):
    """The appointment an upload is filed against, or ``None``.

    Resolved through ``Booking.objects.for_actor`` and then narrowed to the case
    the upload is going onto, so an id belonging to another case — or to a
    counselee's spouse — finds nothing. Two scopes rather than one, because this
    is the only place a booking id arrives from a query string.

    A bad id is ignored rather than a 404. The link is a label on the document;
    the upload is the thing the counselee came to do, and refusing the file
    because a stale link was followed would be the wrong way round.
    """
    raw = request.POST.get("booking") or request.GET.get("booking") or ""
    if not looks_like_public_id(raw):
        return None
    return Booking.objects.for_actor(request.user).filter(case=case, public_id=raw).first()


@login_required
@require_http_methods(["GET", "POST"])
def upload(request, case_public_id):
    case = visible_case_or_404(request, case_public_id)
    require_perm(request, "documents.add_document", case)

    # Only the counselor decides what the whole case sees, so the visibility field
    # is not offered to a counselee at all — see documents.share_document.
    may_share = request.user.has_perm("counseling.change_case", case)
    form = DocumentUploadForm(
        request.POST or None,
        request.FILES or None,
        may_choose_visibility=may_share,
    )
    # Carried on the query string in and a hidden field back out, rather than being
    # a form field: nobody is choosing it, and a select of "your appointments" on
    # the upload page would list a joint session on a family case to whoever opened
    # it.
    booking = session_being_uploaded_for(request, case)

    if request.method == "POST" and form.is_valid():
        try:
            document = services.store_document(
                case=case,
                owner=request.user,
                upload=form.cleaned_data["file"],
                title=form.cleaned_data["title"],
                description=form.cleaned_data["description"],
                kind=form.cleaned_data["kind"],
                visibility=(
                    form.cleaned_data.get("visibility") if may_share else Visibility.PRIVATE
                ),
                booking=booking,
                request=request,
            )
        except services.UploadRejected as exc:
            form.add_error("file", str(exc))
        else:
            messages.success(
                request,
                _("“%(name)s” has been uploaded.") % {"name": document.display_name},
            )
            # Back where they came from when they came from an appointment: the
            # page then shows the document filed against that session, which is
            # the confirmation somebody uploading homework actually wants.
            if booking:
                return redirect("scheduling:detail", public_id=booking.public_id)
            return redirect("documents:case_documents", case_public_id=case.public_id)

    return render(
        request,
        "documents/upload.html",
        {"form": form, "case": case, "may_share": may_share, "booking": booking},
    )


def the_document_after(document, *, actor):
    """The next document on this case, as the case's list would hand it over.

    "Next" means the next row of ``/cases/<id>/documents/``, which lists newest
    first — so it is the one uploaded just *before* this one. Read literally, "the
    next uploaded document" would be the newer one, and that is the wrong end for the
    journey this exists for: somebody opens the top of a list of eleven documents and
    wants the other ten without returning to the list between each.

    Resolved through ``for_actor``, which is what makes it safe to offer at all: what
    it steps over is whatever this actor could not have opened anyway, so a counselee
    walking a case's documents never learns that a private one sits between two of
    theirs, and no count of them is implied either.

    The primary key breaks a tie on the timestamp. Several documents uploaded in one
    sitting can share one to the microsecond, and without a total order a pair of them
    would point at each other — a Next button that goes back where it came from.
    """
    return (
        Document.objects.for_actor(actor)
        .filter(case_id=document.case_id)
        .filter(
            Q(created_at__lt=document.created_at)
            | Q(created_at=document.created_at, pk__lt=document.pk)
        )
        .order_by("-created_at", "-pk")
        .first()
    )


@login_required
def detail(request, public_id):
    document = visible_document_or_404(request, public_id)
    require_perm(request, "documents.view_document", document)
    record(
        AuditVerb.DOCUMENT_VIEWED,
        actor=request.user,
        target=document,
        request=request,
        case_id=document.case_id,
    )
    return render(
        request,
        "documents/detail.html",
        {
            "document": document,
            "next_document": the_document_after(document, actor=request.user),
            "can_change": request.user.has_perm("documents.change_document", document),
            "can_share": request.user.has_perm("documents.share_document", document),
            "can_delete": request.user.has_perm("documents.delete_document", document),
        },
    )


@login_required
def download(request, public_id):
    """Stream the decrypted document.

    The permission check and the audit row are inside ``open_document`` rather than
    here, so nothing can serve a document without recording that it did.
    """
    document = visible_document_or_404(request, public_id)

    try:
        frames = services.open_document(document, actor=request.user, request=request)
    except FileNotFoundError:
        # The row exists and the blob does not. A restore that missed the document
        # volume looks exactly like this, so it is logged loudly rather than
        # shown as an ordinary 404.
        logger.error("Document %s has no stored blob (%s)", document.pk, document.storage_key)
        raise Http404 from None
    except DecryptionError:
        logger.error("Document %s failed to decrypt", document.pk)
        raise Http404 from None

    return encrypted_file_response(
        frames,
        filename=document.original_filename,
        content_type=document.content_type,
        byte_size=document.byte_size,
    )


@login_required
def preview(request, public_id):
    """Serve the document for the browser to render, rather than to save.

    The counselor's actual request: read what was sent in without a folder full of
    counselee files accumulating in Downloads on a shared church computer. Nothing
    about the access is softer than a download — the whole file is handed over — so
    it goes through the same ``open_document``, with the same permission re-check
    and the same audit row. Calling it something gentler in the trail would
    understate what happened.

    Two things stand between "inline" and serving a counselee's uploaded SVG as our
    own origin: the type must be on the allowlist in ``apps/core/downloads.py``, and
    the plaintext's first bytes must agree with it. Anything else is a 404 rather
    than a silent fallback to a download, because a page that offers "View" and
    quietly saves a file instead has told the user something untrue.
    """
    document = visible_document_or_404(request, public_id)

    try:
        frames = services.open_document(document, actor=request.user, request=request)
        head = next(frames, b"")
    except FileNotFoundError:
        logger.error("Document %s has no stored blob (%s)", document.pk, document.storage_key)
        raise Http404 from None
    except DecryptionError:
        logger.error("Document %s failed to decrypt", document.pk)
        raise Http404 from None

    if not may_be_shown_inline(document.content_type, head):
        raise Http404

    return inline_file_response(
        chain([head], frames),
        filename=document.original_filename,
        content_type=document.content_type,
        byte_size=document.byte_size,
    )


@login_required
def thumbnail(request, public_id):
    """The decrypted preview, served inline.

    Inline is safe here and only here: the bytes are a JPEG we produced ourselves
    by re-encoding through Pillow, so the content type is a fact rather than a
    claim. Nothing the uploader sent survives into this response — which is as true
    of a PDF's rendered first page as of a photograph, since both leave
    ``images.make_thumbnail`` as a small JPEG and nothing else is ever stored here.
    """
    document = visible_document_or_404(request, public_id)
    require_perm(request, "documents.view_document", document)

    if not document.has_thumbnail:
        raise Http404

    try:
        data = services.open_thumbnail(document)
    except (FileNotFoundError, DecryptionError):
        raise Http404 from None

    response = HttpResponse(data, content_type="image/jpeg")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store, max-age=0"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def edit(request, public_id):
    document = visible_document_or_404(request, public_id)
    require_perm(request, "documents.change_document", document)

    form = DocumentEditForm(request.POST or None, instance=document)
    if request.method == "POST" and form.is_valid():
        form.save()
        record(
            AuditVerb.DOCUMENT_UPDATED,
            actor=request.user,
            target=document,
            request=request,
            case_id=document.case_id,
            fields=sorted(form.changed_data),
        )
        messages.success(request, _("Saved."))
        return redirect("documents:detail", public_id=document.public_id)

    return render(request, "documents/edit.html", {"form": form, "document": document})


@login_required
@require_POST
def share(request, public_id):
    """Share a document with everyone on the case, or take it back.

    One view for both directions because they are one decision with two settings,
    and splitting them would let a template offer the wrong one.
    """
    document = visible_document_or_404(request, public_id)
    require_perm(request, "documents.share_document", document)

    if document.is_shared_with_the_case:
        services.unshare(document, actor=request.user, request=request)
        messages.success(request, _("No longer shared with the case."))
    else:
        services.share_with_case(document, actor=request.user, request=request)
        messages.warning(
            request,
            _(
                "Shared. Everyone currently on this case can now read it, and "
                "anyone who reads it keeps what they have read."
            ),
        )
    return redirect("documents:detail", public_id=document.public_id)


@login_required
@require_POST
def delete(request, public_id):
    document = visible_document_or_404(request, public_id)
    require_perm(request, "documents.delete_document", document)

    case_public_id = document.case.public_id
    services.soft_delete_document(document, actor=request.user, request=request)
    messages.success(request, _("Withdrawn. Your counselor has a record that it existed."))
    return redirect("documents:case_documents", case_public_id=case_public_id)


# --- the template library -------------------------------------------------


def visible_template_or_404(request, public_id):
    """The single door onto a DocumentTemplate.

    ``for_actor`` again, which for this model means "staff who counsel, and nobody
    else": a counselee or a financial administrator gets a 404 on every one of these
    routes rather than a refusal, because for them the library does not exist. See
    ``DocumentTemplateQuerySet``.
    """
    return get_object_or_404(
        DocumentTemplate.objects.for_actor(request.user).select_related("uploaded_by"),
        public_id=public_id,
    )


def case_the_library_was_opened_from(request):
    """The case a counselor came to the library *for*, or ``None``.

    Carried on the query string so that the same library page can serve both errands
    — browsing the shelf, and fetching something for a case — without a second view
    or a duplicated search box. The id is resolved through ``Case.objects.for_actor``
    and then permission-checked, so all the ``?case=`` in a copied URL can do is
    offer buttons the actor was already entitled to.

    A malformed id is ignored rather than refused: the library is a page worth
    landing on either way, and a 404 for a stale link would hide the shelf as well as
    the buttons.
    """
    raw = request.GET.get("case", "")
    if not looks_like_public_id(raw):
        return None
    case = Case.objects.for_actor(request.user).filter(public_id=raw).first()
    if case is None or not request.user.has_perm("documents.use_document_template", case):
        return None
    return case


@login_required
def template_library(request):
    """The shelf: every template, searchable, with the whole ministry's copy of each.

    The search is a plain GET form and a single ``icontains`` pass — see
    ``DocumentTemplateQuerySet.matching``. No ranking, no stemming: there are tens of
    these, not thousands, and a counselor who types "intake" wants every row with
    the word in it.
    """
    require_perm(request, "documents.view_document_templates")

    term = request.GET.get("q", "")
    templates = DocumentTemplate.objects.for_actor(request.user).matching(term)
    case = case_the_library_was_opened_from(request)

    return render(
        request,
        "documents/template_library.html",
        {
            "templates": templates,
            "q": term,
            "case": case,
            "can_manage": request.user.has_perm("documents.manage_document_templates"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def template_upload(request):
    require_perm(request, "documents.manage_document_templates")

    form = DocumentTemplateUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            template = services.store_template(
                uploaded_by=request.user,
                upload=form.cleaned_data["file"],
                name=form.cleaned_data["name"],
                description=form.cleaned_data["description"],
                kind=form.cleaned_data["kind"],
                request=request,
            )
        except services.UploadRejected as exc:
            form.add_error("file", str(exc))
        else:
            messages.success(
                request,
                _("“%(name)s” is in the library. Every counselor can use it now.")
                % {"name": template.name},
            )
            return redirect("documents:template_library")

    return render(request, "documents/template_upload.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def template_edit(request, public_id):
    template = visible_template_or_404(request, public_id)
    require_perm(request, "documents.manage_document_templates", template)

    form = DocumentTemplateEditForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        form.save()
        record(
            AuditVerb.DOCUMENT_TEMPLATE_UPDATED,
            actor=request.user,
            target=template,
            request=request,
            fields=sorted(form.changed_data),
        )
        messages.success(request, _("Saved."))
        return redirect("documents:template_library")

    return render(request, "documents/template_edit.html", {"form": form, "template": template})


@login_required
@require_POST
def template_withdraw(request, public_id):
    template = visible_template_or_404(request, public_id)
    require_perm(request, "documents.manage_document_templates", template)

    services.withdraw_template(template, actor=request.user, request=request)
    messages.success(
        request,
        # Said out loud because it is the question an administrator replacing a form
        # will ask next, and the answer is the reason the copies exist.
        _("Withdrawn from the library. Copies already on a case are not affected."),
    )
    return redirect("documents:template_library")


@login_required
def template_download(request, public_id):
    """Hand a counselor the file itself — to print, or to fill in by hand.

    The permission check and the audit row are inside ``open_template``, the way
    ``download`` keeps them inside ``open_document``.
    """
    template = visible_template_or_404(request, public_id)

    try:
        frames = services.open_template(template, actor=request.user, request=request)
    except FileNotFoundError:
        logger.error("Template %s has no stored blob (%s)", template.pk, template.storage_key)
        raise Http404 from None
    except DecryptionError:
        logger.error("Template %s failed to decrypt", template.pk)
        raise Http404 from None

    return encrypted_file_response(
        frames,
        filename=template.original_filename,
        content_type=template.content_type,
        byte_size=template.byte_size,
    )


@login_required
def template_thumbnail(request, public_id):
    """The library's preview, served inline. As ``thumbnail`` above, and as safe:
    the bytes are a JPEG this application re-encoded itself."""
    template = visible_template_or_404(request, public_id)
    require_perm(request, "documents.view_document_templates")

    if not template.has_thumbnail:
        raise Http404

    try:
        data = services.open_thumbnail(template)
    except (FileNotFoundError, DecryptionError):
        raise Http404 from None

    response = HttpResponse(data, content_type="image/jpeg")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store, max-age=0"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def template_use(request, case_public_id, public_id):
    """Copy a template onto a case, under a name the counselor chooses.

    The case is resolved and permission-checked *before* the template, which is the
    order the two refusals should arrive in: somebody who may not put documents on
    this case gets a 403 about the case rather than a 404 that also tells them
    whether the template exists.
    """
    case = visible_case_or_404(request, case_public_id)
    require_perm(request, "documents.use_document_template", case)
    template = visible_template_or_404(request, public_id)

    form = UseTemplateForm(request.POST or None, initial={"name": template.name})
    if request.method == "POST" and form.is_valid():
        try:
            document = services.use_template(
                template,
                case=case,
                actor=request.user,
                title=form.cleaned_data["name"],
                visibility=form.cleaned_data["visibility"],
                request=request,
            )
        except services.UploadRejected as exc:
            # Reachable if the scanner has learned a signature since the template was
            # stored, or if it is unreachable. Shown rather than swallowed: the
            # counselor needs to know the handout did not arrive on the case.
            form.add_error(None, str(exc))
        else:
            messages.success(
                request,
                _("“%(name)s” has been added to %(case)s.")
                % {"name": document.display_name, "case": case.label},
            )
            return redirect("documents:detail", public_id=document.public_id)

    return render(
        request,
        "documents/template_use.html",
        {"form": form, "case": case, "template": template},
    )
