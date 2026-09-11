"""
Starting, answering, and closing a conversation.

Kept out of the views so the rules are testable without HTTP, and so a later
command — a digest, a retention job — can act without a request object.

Three things here are worth reading before changing anything.

**The audience is set once, when the thread is created, and never widened.**
``start_thread`` writes exactly two participant rows: the counselee the
conversation concerns and the counselor carrying the case. There is no "add
someone" function, because there is no version of that action a counselee has
agreed to. A reassigned counselor is the one exception and it is not a widening —
see ``ensure_participant``.

**Audit metadata never contains a message body.** It carries a character count.
An audit row is readable by a different route than a message is, and copying the
words into it would put the conversation outside the rules that govern the
conversation.

**Email failure never loses a message.** Notifications go through ``notify``,
which logs and swallows, for the same reason booking notifications do.

**An attachment that cannot be stored takes the message down with it.** Files are
accepted, scanned, and encrypted *before* the message row is written, and the
whole thing is one transaction — so a sender never ends up with their words
delivered next to a file that silently did not arrive. Reading one back goes
through ``open_attachment``, which uses ``record_or_raise`` exactly as documents
do: if the disclosure cannot be logged, it does not happen.
"""

import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import Role
from apps.audit.models import AuditVerb
from apps.audit.services import record, record_or_raise
from apps.documents import ingest
from apps.messaging import notify
from apps.messaging.models import (
    MESSAGE_MAX_ATTACHMENTS,
    MESSAGE_MAX_CHARACTERS,
    Message,
    MessageAttachment,
    Participant,
    Thread,
)

logger = logging.getLogger(__name__)


class MessagingError(Exception):
    """A refusal whose message is safe to show the person acting."""


def _clean_body(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise MessagingError(_("A message needs something in it."))
    if len(body) > MESSAGE_MAX_CHARACTERS:
        raise MessagingError(
            _("That message is too long. The limit is %(limit)s characters.")
            % {"limit": MESSAGE_MAX_CHARACTERS}
        )
    return body


def _accept_uploads(uploads, *, author, thread=None, case=None, request=None):
    """Validate, scan, and strip every file before a word of the message is written.

    Deliberately outside the transaction that writes the message, for the two
    reasons documents gives: a rejection has to be *recorded* rather than rolled
    back, and scanning is a network call that has no business holding a database
    transaction open.

    Returns accepted plaintext, which the caller then seals inside its
    transaction. Refusals arrive as ``MessagingError`` so the reply form can show
    them beside the message the sender is still holding, rather than as a stack
    trace over lost words. ``ScannerUnavailable`` is left to propagate: a scanner
    that is down is an operational failure and must not be reported to a counselee
    as a bad file.
    """
    uploads = [upload for upload in uploads if upload]
    if not uploads:
        return []
    if len(uploads) > MESSAGE_MAX_ATTACHMENTS:
        raise MessagingError(
            _("You can send at most %(limit)s files with one message.")
            % {"limit": MESSAGE_MAX_ATTACHMENTS}
        )

    target = thread if thread is not None else case
    case_id = thread.case_id if thread is not None else getattr(case, "pk", None)

    accepted = []
    for upload in uploads:

        def rejected_by_the_scanner(exc, upload=upload):
            record(
                AuditVerb.MESSAGE_ATTACHMENT_REJECTED,
                actor=author,
                target=target,
                request=request,
                case_id=case_id,
                filename=upload.name,
                signature=exc.signature,
            )

        try:
            accepted.append(ingest.accept(upload, on_infected=rejected_by_the_scanner))
        except ingest.UploadRejected as exc:
            raise MessagingError(str(exc)) from exc
    return accepted


def _seal_attachments(message, accepted, written, *, request=None):
    """Encrypt and record each accepted file against a message that now exists.

    ``written`` collects storage keys so the caller can remove the ciphertext if
    the transaction does not survive — the same bargain ``store_document`` makes,
    and the reason the audit row goes inside the transaction with the row it
    describes.
    """
    for item in accepted:
        attachment = MessageAttachment(
            message=message,
            original_filename=item.filename,
            content_type=item.content_type,
            byte_size=item.byte_size,
            sha256=item.sha256,
            scan_status=item.scan_status,
            scan_detail=item.scan_detail,
        )
        blob = ingest.seal(item.data, storage_key=attachment.storage_key)
        attachment.wrapped_dek = blob.wrapped_dek
        attachment.dek_nonce = blob.dek_nonce
        written.append(attachment.storage_key)
        attachment.save()
        record(
            AuditVerb.MESSAGE_ATTACHMENT_ADDED,
            actor=message.author,
            target=attachment,
            request=request,
            case_id=message.thread.case_id,
            thread_id=str(message.thread_id),
            filename=attachment.original_filename,
            content_type=attachment.content_type,
            byte_size=attachment.byte_size,
            scan_status=attachment.scan_status,
        )


def _append(thread, *, author, body):
    """Add one message and move the thread's clock forward.

    The author's own read marker moves too: they have plainly read what they just
    wrote, and leaving it behind would show them their own message as unread.
    """
    message = Message.objects.create(thread=thread, author=author, body=body)
    thread.last_message_at = message.created_at
    thread.save(update_fields=["last_message_at", "updated_at"])
    Participant.objects.filter(thread=thread, user=author).update(last_read_at=message.created_at)
    return message


def start_thread(*, case, author, subject, body, counselee=None, uploads=(), request=None):
    """Open a conversation between the case counselor and one counselee.

    ``counselee`` says who it is with and is required when a counselor starts it;
    when a counselee starts it, it is them, and passing anyone else is refused
    rather than ignored.

    Both participant rows are written here, so the audience of the conversation is
    a fact about the thread from the moment it exists rather than something
    inferred later from who has replied.
    """
    body = _clean_body(body)
    subject = (subject or "").strip()
    if not subject:
        raise MessagingError(_("A conversation needs a subject."))

    if author.role == Role.COUNSELEE:
        if counselee is not None and counselee.pk != author.pk:
            raise MessagingError(_("You can only start a conversation with your counselor."))
        counselee = author
    elif author.pk != case.counselor_id:
        # Reachable only by a programming mistake — messaging.add_thread already
        # refuses everyone else, an administrator included. Checked anyway,
        # because this function is what writes the audience.
        raise MessagingError(_("Only this case's counselor may start a conversation on it."))

    if counselee is None:
        raise MessagingError(_("Choose who the conversation is with."))
    if not case.members.filter(counselee=counselee, ended_on__isnull=True).exists():
        raise MessagingError(_("That person is not currently on this case."))

    accepted = _accept_uploads(uploads, author=author, case=case, request=request)

    written: list = []
    try:
        with transaction.atomic():
            thread = Thread.objects.create(case=case, subject=subject, started_by=author)
            Participant.objects.create(thread=thread, user=counselee)
            # Two rows even when the counselor is the author. get_or_create rather
            # than create for the case a counselor is somehow also the counselee,
            # which the role constraints forbid but which would otherwise raise a
            # unique violation instead of a sentence.
            Participant.objects.get_or_create(thread=thread, user=case.counselor)
            message = _append(thread, author=author, body=body)
            record(
                AuditVerb.THREAD_STARTED,
                actor=author,
                target=thread,
                request=request,
                case_id=case.pk,
                counselee_id=str(counselee.pk),
                counselor_id=str(case.counselor_id),
                characters=len(body),
                attachments=len(accepted),
            )
            _seal_attachments(message, accepted, written, request=request)
    except BaseException:
        ingest.discard(written)
        raise

    notify.new_message(message)
    return thread


def post_message(thread, *, author, body, uploads=(), request=None):
    """Add a reply to an existing conversation."""
    body = _clean_body(body)
    if not thread.is_open:
        raise MessagingError(_("This conversation has been closed."))

    accepted = _accept_uploads(uploads, author=author, thread=thread, request=request)

    # A counselor who inherited the case may not have a row yet, and their reply
    # is the moment they join.
    ensure_participant(thread, user=author)

    written: list = []
    try:
        with transaction.atomic():
            message = _append(thread, author=author, body=body)
            record(
                AuditVerb.MESSAGE_SENT,
                actor=author,
                target=message,
                request=request,
                case_id=thread.case_id,
                thread_id=str(thread.pk),
                characters=len(body),
                attachments=len(accepted),
            )
            _seal_attachments(message, accepted, written, request=request)
    except BaseException:
        ingest.discard(written)
        raise

    notify.new_message(message)
    return message


def open_attachment(attachment, *, actor, request=None):
    """Return a generator of plaintext frames, after recording the access.

    A copy of ``documents.services.open_document`` in its two load-bearing
    respects, and copied on purpose rather than shared: this is the function that
    turns a counselee's file back into plaintext, so it re-checks the permission
    itself instead of taking the view's word for it, and it records the read with
    ``record_or_raise`` — an unlogged disclosure is the thing the audit trail
    exists to prevent, so a trail that cannot be written costs the download.
    """
    if not actor.has_perm("messaging.view_attachment", attachment):
        record(
            AuditVerb.ACCESS_DENIED,
            actor=actor,
            target=attachment,
            request=request,
            case_id=attachment.message.thread.case_id,
            permission="messaging.view_attachment",
        )
        raise PermissionDenied

    record_or_raise(
        AuditVerb.MESSAGE_ATTACHMENT_DOWNLOADED,
        actor=actor,
        target=attachment,
        request=request,
        case_id=attachment.message.thread.case_id,
        thread_id=str(attachment.message.thread_id),
        filename=attachment.original_filename,
        byte_size=attachment.byte_size,
    )

    return ingest.unseal(
        storage_key=attachment.storage_key,
        wrapped_dek=attachment.wrapped_dek,
        dek_nonce=attachment.dek_nonce,
    )


def ensure_participant(thread, *, user):
    """Return this person's participant row, creating the counselor's if needed.

    The one place a thread's audience can change, and it is not a widening: a case
    reassigned to a new counselor carries its correspondence with it, exactly as
    its documents do, and the new counselor can already read the thread through
    ``ThreadQuerySet.scope_for_counselor``. The row is what lets them have an
    unread count.

    Returns None for anyone else — an administrator reading a case leaves no
    participant row, because they are not in the conversation.
    """
    existing = thread.participants.filter(user=user).first()
    if existing is not None:
        return existing
    if user.pk == thread.case.counselor_id:
        participant, _created = Participant.objects.get_or_create(thread=thread, user=user)
        return participant
    return None


def mark_read(thread, *, user):
    """Record that this person has read the conversation up to now.

    Called when the thread is rendered. Idempotent, and a no-op for anyone
    without a place in the conversation.
    """
    participant = ensure_participant(thread, user=user)
    if participant is None:
        return None
    participant.last_read_at = timezone.now()
    participant.save(update_fields=["last_read_at", "updated_at"])
    return participant


def close_thread(thread, *, actor, request=None):
    """End a conversation without ending what is in it.

    Idempotent: closing something already closed is a double-clicked button.
    """
    if not thread.is_open:
        return thread
    thread.closed_at = timezone.now()
    thread.save(update_fields=["closed_at", "updated_at"])
    record(
        AuditVerb.THREAD_CLOSED,
        actor=actor,
        target=thread,
        request=request,
        case_id=thread.case_id,
    )
    return thread


def reopen_thread(thread, *, actor, request=None):
    if thread.is_open:
        return thread
    thread.closed_at = None
    thread.save(update_fields=["closed_at", "updated_at"])
    record(
        AuditVerb.THREAD_REOPENED,
        actor=actor,
        target=thread,
        request=request,
        case_id=thread.case_id,
    )
    return thread


# --- unread counts --------------------------------------------------------
#
# All three functions below go through MessageQuerySet.unread_for, so the badge
# in the header, the count on a case, and the count beside a thread cannot
# disagree with each other.


def unread_total(user) -> int:
    """How many messages are waiting for this person, across every case.

    Zero for the roles that have no place in a conversation. An administrator can
    read every thread in the ministry, so counting what they have not opened
    would produce a number in the hundreds that means nothing — and a header
    badge is a nudge to reply, which is not an administrator's job here.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    if user.role not in (Role.COUNSELOR, Role.COUNSELEE):
        return 0
    return Message.objects.for_actor(user).unread_for(user).count()


def unread_counts_by_thread(user) -> dict[int, int]:
    rows = (
        Message.objects.for_actor(user)
        .unread_for(user)
        .order_by()
        .values("thread")
        .annotate(waiting=Count("pk"))
    )
    return {row["thread"]: row["waiting"] for row in rows}


def with_unread(threads, user):
    """Attach ``unread_count`` to each thread in one extra query.

    Returns a list, because the whole point is that the attribute survives — an
    annotation would be lost by a template re-evaluating the queryset, and a
    per-row count would be one query per conversation.
    """
    threads = list(threads)
    counts = unread_counts_by_thread(user)
    for thread in threads:
        thread.unread_count = counts.get(thread.pk, 0)
    return threads
