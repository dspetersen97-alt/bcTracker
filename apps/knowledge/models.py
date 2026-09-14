"""
The knowledge base: what the ministry has learned, on one shelf.

This is the counselors' own shelf, and that is what distinguishes it from the
template library in ``apps/documents``. A template is blank paperwork an
administrator decides is current; a resource here is something a counselor found
useful — a handout on anxiety they wrote, an article worth reading before a grief
case, a book they recommend — and every counselor may add one and comment on
anybody's.

Two consequences run through this module:

  * **A resource is a file, a link, or both.** Those are genuinely the same thing
    from a counselor's point of view ("what have we got on anxiety?"), so they are
    one model and one search rather than two lists somebody has to check in turn.
    The file half is stored exactly like a document — scanned, encrypted under its
    own DEK — through the same ``ingest`` pipeline, because "the knowledge base is
    the unencrypted corner of the volume" is not a distinction worth keeping.
  * **Counselees cannot reach it at all.** ``ResourceQuerySet`` gives them
    ``none()``, and billing too. A handout is not a secret, but this shelf carries
    the ministry's working notes on how to counsel people, including comments
    counselors leave for each other, and none of that is written to be read by the
    person being counselled. What reaches a counselee is a document on their case.

Comments live here rather than in messaging for the reason a case note is not a
message: nobody is being written *to*. A comment is an annotation on the resource,
readable by everybody who can read the resource, and it is soft-deleted like
everything else so that removing one cannot leave a reply answering nothing.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import (
    PublicIdModel,
    SoftDeleteModel,
    SoftDeleteQuerySet,
    TimeStampedModel,
)
from apps.core.scoping import ActorScopedQuerySet
from apps.documents.models import ScanStatus


class ResourceKind(models.TextChoices):
    """What sort of thing this is, in a counselor's words rather than a file's.

    Deliberately not the same list as ``DocumentKind``: that one describes a file on
    a case ("intake paperwork", "correspondence"), and this one describes something
    on a shelf. Overlapping the two would make ``kind`` mean two things.
    """

    HANDOUT = "handout", _("Handout or worksheet")
    ARTICLE = "article", _("Article or reading")
    BOOK = "book", _("Book or booklet")
    TRAINING = "training", _("Training or talk")
    OTHER = "other", _("Other")


class ResourceQuerySet(ActorScopedQuerySet, SoftDeleteQuerySet):
    """Who may see the shelf at all.

    No ``case_path``: a resource is about a subject, never about a person, so there
    is nothing to scope through. Staff who counsel see everything; the other two
    roles see nothing, which is the same shape as the template library's rule and
    for a related reason — except that here the counselee's ``none()`` matters more,
    because these rows carry counselors' notes to each other rather than blank forms.
    """

    def matching(self, term):
        """Narrow to the resources a search term describes.

        Title, summary, topics, the link, the filename, **and the comments**. The
        comments are the part worth explaining: the whole point of letting counselors
        annotate a resource is that the useful sentence is often not the contributor's
        — "we used this one for anxiety and it landed well" is written underneath, and
        a search for "anxiety" that missed it would be missing the best thing on the
        page. Soft-deleted comments are excluded in the same ``Q`` so a removed
        comment cannot keep pulling a resource into results.

        One ``icontains`` pass and no ranking, as in the template library: there are
        tens of these, not thousands.
        """
        term = (term or "").strip()
        if not term:
            return self
        return self.filter(
            models.Q(title__icontains=term)
            | models.Q(summary__icontains=term)
            | models.Q(topics__icontains=term)
            | models.Q(url__icontains=term)
            | models.Q(original_filename__icontains=term)
            | models.Q(comments__body__icontains=term, comments__deleted_at__isnull=True)
            # A resource with three matching comments is one resource.
        ).distinct()

    def scope_for_admin(self, user):
        return self

    def scope_for_counselor(self, user):
        # Every counselor sees every resource, including the ones somebody else
        # contributed. A per-counselor shelf would be a filing cabinet, and the point
        # of this feature is that what one counselor learned reaches the others.
        return self


class ResourceManager(models.Manager.from_queryset(ResourceQuerySet)):
    def get_queryset(self):
        return super().get_queryset().alive()

    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class Resource(PublicIdModel, SoftDeleteModel, TimeStampedModel):
    """One thing on the shelf: an uploaded file, a link, or both.

    Both is not a compromise between the two — it is a handout and the page it came
    from, which is how a counselor actually holds a resource in mind. The constraint
    below insists on at least one of them, because a row with neither is a title
    nobody can act on.

    Removing one is a soft delete, as everywhere else. Comments hang off it, and a
    thread of them that suddenly refers to nothing would be worse than a row marked
    withdrawn.
    """

    title = models.CharField(
        max_length=200,
        help_text=_("What a counselor would call it. “Handling anxious thoughts”."),
    )
    summary = models.TextField(
        blank=True,
        help_text=_("What it is, and when you would reach for it. Searched with the title."),
    )
    #: Free text rather than a Tag model, and searched by ``icontains``. A tag table
    #: earns its keep when tags are browsed, renamed and counted; here they are typed
    #: once and read by a search box, and the table would mostly be a page of
    #: near-duplicates for somebody to tidy.
    topics = models.CharField(
        max_length=200,
        blank=True,
        help_text=_("Comma separated, in whatever words you would search for: anxiety, grief."),
    )
    kind = models.CharField(max_length=20, choices=ResourceKind.choices, default=ResourceKind.OTHER)

    url = models.URLField(
        blank=True,
        max_length=500,
        help_text=_("A link to the article, video or shop page, if there is one."),
    )

    # PROTECT, as every other "who did this" FK here is: an account with history is
    # deactivated, never deleted, and who contributed a resource is part of the shelf.
    contributed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="knowledge_resources",
    )

    # --- the file half, all optional: a link-only resource has none of it ---
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    byte_size = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)

    storage_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    wrapped_dek = models.BinaryField(null=True, blank=True, editable=False)
    dek_nonce = models.BinaryField(null=True, blank=True, editable=False)

    scan_status = models.CharField(max_length=20, choices=ScanStatus.choices, blank=True)
    scan_detail = models.CharField(max_length=200, blank=True)

    objects = ResourceManager()
    all_objects = models.Manager.from_queryset(ResourceQuerySet)()

    class Meta:
        # Newest first, unlike the template library's alphabetical shelf. A library is
        # read by looking for a known form; this page is read to see what colleagues
        # have added, and search is how somebody looks for a known one.
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["kind"]),
        ]
        constraints = [
            # A title with nothing behind it is not a resource. Checked in the
            # database as well as in the form, because the service layer can be
            # called from a management command and a shell.
            models.CheckConstraint(
                condition=~models.Q(url="") | models.Q(storage_key__isnull=False),
                name="knowledge_resource_has_a_file_or_a_link",
            ),
            # The file half is all-or-nothing: a storage key without a wrapped DEK is
            # ciphertext nobody can open, and a DEK without a key is a dangling one.
            models.CheckConstraint(
                condition=models.Q(storage_key__isnull=True, wrapped_dek__isnull=True)
                | models.Q(storage_key__isnull=False, wrapped_dek__isnull=False),
                name="knowledge_resource_file_is_whole",
            ),
            models.CheckConstraint(
                condition=models.Q(byte_size__isnull=True) | models.Q(byte_size__gt=0),
                name="knowledge_resource_file_is_not_empty",
            ),
            # As on Document and DocumentTemplate: an infected file never reaches the
            # volume, so a row in this state would mean a bug upstream, and refusing
            # it here means that bug cannot end with malware stored.
            models.CheckConstraint(
                condition=~models.Q(scan_status=ScanStatus.INFECTED),
                name="infected_resources_are_never_stored",
            ),
        ]
        permissions = []

    def __str__(self) -> str:
        return self.title

    @property
    def has_file(self) -> bool:
        return self.storage_key is not None

    @property
    def has_link(self) -> bool:
        return bool(self.url)


class ResourceCommentQuerySet(ActorScopedQuerySet, SoftDeleteQuerySet):
    def for_actor(self, user):
        """The resource decides, and nothing else.

        The whole access rule for a comment, written the way messaging writes an
        attachment's: there is no state on a comment that could make it more or less
        visible than the thing it is written on.
        """
        return self.filter(resource__in=Resource.objects.for_actor(user))


class ResourceCommentManager(models.Manager.from_queryset(ResourceCommentQuerySet)):
    def get_queryset(self):
        return super().get_queryset().alive()

    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class ResourceComment(PublicIdModel, SoftDeleteModel, TimeStampedModel):
    """What one counselor wants the others to know about a resource.

    Not editable, only removable — the same choice messaging makes about a message,
    and for a weaker but real version of the same reason: colleagues act on what they
    read here, and a comment that can be quietly rewritten afterwards is a note
    nobody can rely on having read.
    """

    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="knowledge_comments",
    )
    body = models.TextField()

    objects = ResourceCommentManager()
    all_objects = models.Manager.from_queryset(ResourceCommentQuerySet)()

    class Meta:
        # Oldest first: this is a conversation, and a conversation is read downwards.
        ordering = ["created_at"]
        indexes = [models.Index(fields=["resource", "created_at"])]
        permissions = []

    def __str__(self) -> str:
        return f"{self.author} on {self.resource}"
