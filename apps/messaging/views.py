"""
Messaging views.

Every one of them resolves its object through ``for_actor`` and then re-checks a
permission, the pattern the documents app sets. Two things specific to this app:

**Reading a conversation is audited.** Opening a thread is the same kind of act as
opening a document — somebody has read what a counselee wrote — so ``thread``
records it before rendering. It is also where a read marker moves, which is why
that view is the only one that writes on a GET.

**The reply lives on the thread page.** One route, GET renders and POST appends,
so there is no way to reach a "post a message" endpoint without having been shown
the conversation it belongs to. The permission for writing is checked separately
from the one for reading, which is what lets an administrator read a thread and
be refused when they try to answer it.

**An attachment has its own route and no other.** ``attachment`` below is the only
way bytes leave the encrypted store for a message, and it resolves the row through
``MessageAttachment.objects.for_actor`` — which derives entirely from the thread —
before ``services.open_attachment`` re-checks the permission and records the read.
"""

import logging

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core.downloads import encrypted_file_response
from apps.counseling.models import Case
from apps.documents.crypto import DecryptionError
from apps.messaging import services
from apps.messaging.forms import ReplyForm, ThreadStartForm
from apps.messaging.models import MessageAttachment, Thread

logger = logging.getLogger(__name__)


def visible_case_or_404(request, pk):
    return get_object_or_404(Case.objects.for_actor(request.user), pk=pk)


def visible_attachment_or_404(request, pk):
    """The single door onto a MessageAttachment.

    ``MessageAttachment.objects.for_actor`` filters on
    ``Thread.objects.for_actor``, so there is one rule about who may read a
    conversation and this inherits it — a spouse asking for the id of a file in the
    other's correspondence gets a 404 and learns nothing about whether it exists.
    """
    return get_object_or_404(
        MessageAttachment.objects.for_actor(request.user).select_related(
            "message", "message__thread", "message__thread__case"
        ),
        pk=pk,
    )


def visible_thread_or_404(request, pk):
    """The single door onto a Thread.

    ``for_actor`` is what makes "a spouse cannot reach the other's correspondence"
    true by construction: the row is not in the queryset, so this is a 404 and the
    conversation's existence is never confirmed.
    """
    return get_object_or_404(
        Thread.objects.for_actor(request.user).select_related("case", "case__counselor"),
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


@login_required
def index(request):
    """Every conversation this actor may see, newest activity first.

    Not branched on the role: for a counselee this is their own correspondence,
    for a counselor it is their caseload's, and for an administrator it is the
    ministry's. The scoping layer decides which, so this view does not have to
    know.
    """
    require_perm(request, "messaging.view_thread_index")

    threads = services.with_unread(
        Thread.objects.for_actor(request.user).select_related(
            "case", "case__counselor", "started_by"
        ),
        request.user,
    )
    return render(request, "messaging/index.html", {"threads": threads})


@login_required
def case_threads(request, case_pk):
    """The conversations on one case.

    A counselee sees only their own, because the queryset requires a participant
    row. There is no count of the ones they cannot see, for the reason the
    documents list gives none: a number is content.
    """
    case = visible_case_or_404(request, case_pk)
    require_perm(request, "messaging.view_case_threads", case)

    threads = services.with_unread(
        Thread.objects.for_actor(request.user)
        .filter(case=case)
        .select_related("case", "started_by"),
        request.user,
    )
    return render(
        request,
        "messaging/case_threads.html",
        {
            "case": case,
            "threads": threads,
            "can_start": request.user.has_perm("messaging.add_thread", case),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def start(request, case_pk):
    case = visible_case_or_404(request, case_pk)
    require_perm(request, "messaging.add_thread", case)

    form = ThreadStartForm(
        request.POST or None, request.FILES or None, case=case, author=request.user
    )
    if request.method == "POST" and form.is_valid():
        try:
            thread = services.start_thread(
                case=case,
                author=request.user,
                subject=form.cleaned_data["subject"],
                body=form.cleaned_data["body"],
                counselee=form.cleaned_data.get("counselee"),
                uploads=form.cleaned_data["files"],
                request=request,
            )
        except services.MessagingError as exc:
            form.add_error(None, str(exc))
        else:
            flash.success(request, _("Sent."))
            return redirect("messaging:thread", pk=thread.pk)

    return render(request, "messaging/start.html", {"form": form, "case": case})


@login_required
@require_http_methods(["GET", "POST"])
def thread(request, pk):
    thread = visible_thread_or_404(request, pk)
    require_perm(request, "messaging.view_thread", thread)

    form = ReplyForm(request.POST or None, request.FILES or None)
    if request.method == "POST":
        # Checked here rather than at the top: an administrator may read this page
        # and may not write in it, and a closed conversation refuses everyone.
        require_perm(request, "messaging.add_message", thread)
        if form.is_valid():
            try:
                services.post_message(
                    thread,
                    author=request.user,
                    body=form.cleaned_data["body"],
                    uploads=form.cleaned_data["files"],
                    request=request,
                )
            except services.MessagingError as exc:
                form.add_error("body", str(exc))
            else:
                flash.success(request, _("Sent."))
                return redirect("messaging:thread", pk=thread.pk)

    record(
        AuditVerb.THREAD_VIEWED,
        actor=request.user,
        target=thread,
        request=request,
        case_id=thread.case_id,
    )
    # After the audit row and after the reply, so the conversation is marked read
    # including whatever was just added to it.
    services.mark_read(thread, user=request.user)

    return render(
        request,
        "messaging/thread.html",
        {
            "thread": thread,
            "conversation": thread.messages.select_related("author").prefetch_related(
                "attachments"
            ),
            "participants": [
                participant.user for participant in thread.participants.select_related("user").all()
            ],
            "form": form,
            "can_reply": request.user.has_perm("messaging.add_message", thread),
            "can_close": request.user.has_perm("messaging.close_thread", thread),
            "can_reopen": request.user.has_perm("messaging.reopen_thread", thread),
        },
    )


@login_required
def attachment(request, pk):
    """Stream the decrypted file that came with a message.

    The permission check and the audit row are inside ``open_attachment`` rather
    than here, for the reason ``documents.download`` gives: nothing may serve a
    counselee's file without recording that it did.

    There is no thumbnail route for an attachment even though an image knows it is
    one. A preview is a second plaintext derivative of a file somebody sent in
    confidence, and a filing cabinet is worth that cost in a way a conversation is
    not — the message says what the file is.
    """
    attachment = visible_attachment_or_404(request, pk)

    try:
        frames = services.open_attachment(attachment, actor=request.user, request=request)
    except FileNotFoundError:
        # The row exists and the blob does not — what a restore that missed the
        # documents volume looks like. Logged loudly rather than shown as a plain
        # 404, which would read as "no such file" to whoever is investigating.
        logger.error(
            "Message attachment %s has no stored blob (%s)", attachment.pk, attachment.storage_key
        )
        raise Http404 from None
    except DecryptionError:
        logger.error("Message attachment %s failed to decrypt", attachment.pk)
        raise Http404 from None

    return encrypted_file_response(
        frames,
        filename=attachment.original_filename,
        content_type=attachment.content_type,
        byte_size=attachment.byte_size,
    )


@login_required
@require_POST
def close(request, pk):
    thread = visible_thread_or_404(request, pk)
    require_perm(request, "messaging.close_thread", thread)

    services.close_thread(thread, actor=request.user, request=request)
    flash.success(
        request,
        _("Closed. Nothing has been deleted — it can be reopened if there is more to say."),
    )
    return redirect("messaging:thread", pk=thread.pk)


@login_required
@require_POST
def reopen(request, pk):
    thread = visible_thread_or_404(request, pk)
    require_perm(request, "messaging.reopen_thread", thread)

    services.reopen_thread(thread, actor=request.user, request=request)
    flash.success(request, _("Reopened."))
    return redirect("messaging:thread", pk=thread.pk)
