"""
Internal messaging between a counselor and one counselee.

The shape of a thread is the whole design, so it is worth stating plainly: **a
thread's audience is the case counselor and exactly one counselee.** Not the
case. On a couple's case, spouse A and spouse B each correspond with the
counselor separately and neither sees the other's thread — the same "nothing of
the other's" rule the ``Document`` visibility default implements, except that
here there is no shared setting at all, because there is no version of a private
conversation that makes sense to publish to the room.

That is why ``Participant`` exists rather than the audience being derived from
``CaseMember``: membership says who is in the counseling relationship, and
participation says who is in this conversation. The two are deliberately
different, and access needs both — see ThreadQuerySet.scope_for_counselee, which
requires a *current* membership as well as a participant row.

Three further decisions:

  * ``financial_admin`` gets ``none()``, exactly as with documents. Billing needs
    to know a case exists and who carries it; the correspondence on it is not a
    billing question.
  * An ``admin`` may **read** a thread and may not write in one. That mirrors the
    session note, where an administrator may read what the counselor recorded but
    cannot record it for them. Reading is audited like a document view.
  * Nothing here is soft-deleted, and a ``Message`` cannot be edited after it is
    sent — ``Message.save`` refuses. A message is a record of what was said to a
    counselee, so the way a conversation ends is that the thread is *closed*, and
    what was said stays said.

A counselor sees the threads on their own cases whether or not they hold a
participant row, so a case reassigned to a new counselor carries its
correspondence with it — the same rule documents follow. Their participant row is
created lazily on first read; see services.ensure_participant.

**An attachment is not a Document, on purpose.** ``MessageAttachment`` keeps its
own encrypted blob rather than pointing at ``documents.Document``, because a
Document's audience is decided by its ``visibility`` field: ``PRIVATE`` means the
uploader and the counselor, so a worksheet the *counselor* attached would be
invisible to the counselee it was sent to, and ``CASE_SHARED`` would show a
counselee's private disclosure to their spouse. Neither is what "attached to this
conversation" means. The bytes still go through exactly the same validate-scan-
strip-encrypt pipeline — ``documents/ingest.py`` — so there is one place that
decides what is safe to store; only the question of who may read it differs, and
here the answer is simply "whoever may read the message".
"""

from datetime import UTC, datetime
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import PublicIdModel, TimeStampedModel
from apps.core.scoping import ActorScopedQuerySet, CaseScopedQuerySet
from apps.documents.models import ScanStatus

#: Stands in for "has never opened this thread" when counting what is unread.
#: A NULL ``last_read_at`` compared against a timestamp yields NULL rather than
#: true, which would silently report a thread nobody has opened as fully read.
NEVER_READ = datetime(1970, 1, 1, tzinfo=UTC)

#: Cap on one message, enforced by the forms and by ``services.post_message``.
#: Not a security boundary — it is here so a pasted document arrives as a
#: document, through the encrypted store, rather than as a wall of text in a
#: table nobody can search.
MESSAGE_MAX_CHARACTERS = 10_000

#: How many files may ride along with one message. A cap rather than no limit
#: because each one is scanned and encrypted synchronously while the sender waits,
#: and because a counselee with twenty photographs of a journal is better served by
#: the documents area, which is built for a collection.
MESSAGE_MAX_ATTACHMENTS = 5


class ThreadQuerySet(CaseScopedQuerySet):
    case_path = "case"

    def open(self):
        return self.filter(closed_at__isnull=True)

    def scope_for_counselee(self, user):
        """Their own conversations, and only those.

        Two conditions, both required. The inherited part supplies the *current*
        case membership, so someone whose membership ended stops seeing the
        correspondence. The ``Exists`` supplies participation, which is what
        keeps a spouse out of a thread on a case they are legitimately on.

        ``Exists`` rather than a join through ``participants``, so this cannot
        multiply rows for a caller that goes on to aggregate.
        """
        participates = Participant.objects.filter(thread=models.OuterRef("pk"), user=user)
        return super().scope_for_counselee(user).filter(models.Exists(participates))

    def scope_for_financial_admin(self, user):
        # Nothing, ever — the same decision, and for the same reason, as
        # DocumentQuerySet.scope_for_financial_admin.
        return self.none()


class ThreadManager(models.Manager.from_queryset(ThreadQuerySet)):
    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class Thread(PublicIdModel, TimeStampedModel):
    case = models.ForeignKey(
        "counseling.Case",
        on_delete=models.PROTECT,
        related_name="threads",
    )
    subject = models.CharField(
        max_length=200,
        help_text=_("What this is about. Visible to everyone in the conversation."),
    )
    # PROTECT throughout this app, as with Document.owner: an account that has
    # said something to a counselee is deactivated, never deleted.
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="threads_started",
    )
    # Denormalised so the list can be ordered and read without touching every
    # message. Never null: a thread is always created with its first message.
    last_message_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    objects = ThreadManager()

    class Meta:
        ordering = ["-last_message_at", "-id"]
        indexes = [models.Index(fields=["case", "-last_message_at"])]

    def __str__(self) -> str:
        return self.subject

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class ParticipantQuerySet(ActorScopedQuerySet):
    def for_actor(self, user):
        """Derived from the thread, never restated.

        Deliberately not a set of per-role hooks: participation is only ever
        readable to the extent the thread is, and writing the rules twice is how
        the two come to disagree. The anonymous and inactive checks happen inside
        ``Thread.objects.for_actor``, which returns ``none()`` for both.
        """
        return self.filter(thread__in=Thread.objects.for_actor(user))


class Participant(TimeStampedModel):
    """One person's place in one conversation, and how far they have read."""

    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="thread_participations",
    )
    # Null until they have opened it. See NEVER_READ.
    last_read_at = models.DateTimeField(null=True, blank=True)

    objects = models.Manager.from_queryset(ParticipantQuerySet)()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["thread", "user"], name="uniq_thread_participant"),
        ]
        # Reverse of the FK order: the hot query is "is this person in this
        # thread", asked on every read.
        indexes = [models.Index(fields=["user", "thread"])]

    def __str__(self) -> str:
        return f"{self.user} in {self.thread}"


class MessageQuerySet(ActorScopedQuerySet):
    def for_actor(self, user):
        """As with Participant: the thread decides, so there is one rule."""
        return self.filter(thread__in=Thread.objects.for_actor(user))

    def unread_for(self, user):
        """Messages this person has not seen, in the threads they can reach.

        The single definition of "unread" in the application. Somebody's own
        message is never unread to them, and a thread with no participant row for
        them — a counselor who has just inherited a case — is entirely unread.
        """
        read_up_to = Participant.objects.filter(thread=models.OuterRef("thread"), user=user).values(
            "last_read_at"
        )[:1]
        return self.exclude(author=user).filter(
            created_at__gt=Coalesce(
                models.Subquery(read_up_to, output_field=models.DateTimeField()),
                models.Value(NEVER_READ),
            )
        )


class MessageManager(models.Manager.from_queryset(MessageQuerySet)):
    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class Message(TimeStampedModel):
    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="messages_sent",
    )
    body = models.TextField()

    objects = MessageManager()

    class Meta:
        # Oldest first: this is read as a conversation, not as a feed.
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["thread", "created_at"])]

    def __str__(self) -> str:
        return f"{self.author} at {self.created_at:%Y-%m-%d %H:%M}"

    def save(self, *args, **kwargs):
        """Write once.

        The same policy as an audit row, for a related reason: what a counselee
        was told is a record, and a body that can be rewritten afterwards is not
        one. There is no permission to edit a message and no view that tries, so
        reaching this is a programming mistake — raised here, where the mistake
        is, rather than discovered later as a changed history.
        """
        if self.pk is not None:
            raise ValueError("A sent message cannot be modified.")
        super().save(*args, **kwargs)


class MessageAttachmentQuerySet(ActorScopedQuerySet):
    def for_actor(self, user):
        """The conversation decides, as with Participant and Message.

        This is the whole access rule for an attachment. There is no visibility
        field to get wrong, no sharing to promote, and nothing an attachment is
        readable by that its message is not.
        """
        return self.filter(message__thread__in=Thread.objects.for_actor(user))


class MessageAttachmentManager(models.Manager.from_queryset(MessageAttachmentQuerySet)):
    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class MessageAttachment(PublicIdModel, TimeStampedModel):
    """A file sent with a message. Encrypted on disk under an opaque UUID.

    Deliberately narrower than ``Document``: no title, no description, no kind, no
    thumbnail, and no soft delete. An attachment is part of what was said, so it
    lives and dies with the message — which is to say it does not die, for the same
    reason a message cannot be edited.

    No ``owner`` field either: the uploader is the message's author by definition,
    and a second copy of that fact is a second thing that could disagree with the
    conversation.
    """

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")

    original_filename = models.CharField(max_length=255)
    # Sniffed from the bytes, never taken from the request — a client-supplied
    # content type is an instruction to the browser about how to execute what we
    # hand back.
    content_type = models.CharField(max_length=100)
    byte_size = models.BigIntegerField()
    #: Of the plaintext, after any EXIF strip, so it identifies what a download
    #: will produce.
    sha256 = models.CharField(max_length=64, db_index=True)

    storage_key = models.UUIDField(default=uuid4, unique=True, editable=False)
    wrapped_dek = models.BinaryField(editable=False)
    dek_nonce = models.BinaryField(editable=False)

    scan_status = models.CharField(max_length=20, choices=ScanStatus.choices)
    scan_detail = models.CharField(max_length=200, blank=True)

    objects = MessageAttachmentManager()

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["message", "id"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(byte_size__gt=0), name="message_attachment_is_not_empty"
            ),
            # An infected file is never stored, so a row in this state would mean a
            # bug upstream. Refusing it in the database means that bug cannot end
            # with malware on the volume — the same constraint Document carries.
            models.CheckConstraint(
                condition=~models.Q(scan_status=ScanStatus.INFECTED),
                name="infected_attachments_are_never_stored",
            ),
        ]

    def __str__(self) -> str:
        return self.original_filename

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")
