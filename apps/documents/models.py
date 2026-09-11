"""
Document metadata. The bytes live encrypted on disk under an opaque UUID.

The queryset in this module is the most consequential access rule in the
application, so it is worth reading closely rather than trusting:

  * ``financial_admin`` gets ``none()``. Not a filtered subset — nothing. Billing
    needs to know a case exists and who is on it, which it gets from
    ``counseling``; it has no reason to know a document exists at all.
  * A counselee sees their **own** uploads plus anything the counselor has
    explicitly shared with the whole case. On a couple's case that means a
    spouse's private disclosure is invisible, which is the decision the
    ``visibility`` default exists to implement. Defaulting the other way would
    be a disclosure the ministry could not take back.
  * A counselor sees every document on their own cases. This is what makes the
    two-way exchange work, and it is what the ``PRIVATE`` visibility name means:
    private to the uploader and the counselor, not private from the counselor.

The row keeps the wrapped DEK, not the DEK. Reading a document therefore needs
the master key from the environment as well as database access — a stolen
database dump is metadata, not content.
"""

from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import SoftDeleteModel, SoftDeleteQuerySet, TimeStampedModel
from apps.core.scoping import CaseScopedQuerySet


class Visibility(models.TextChoices):
    # The default, and the one that carries the privacy promise. "Private" here
    # means private from the *other counselees on the case*; the assigned
    # counselor is a party to everything on their case by definition.
    PRIVATE = "private", _("Only me and my counselor")
    CASE_SHARED = "case_shared", _("Everyone on this case")


class DocumentKind(models.TextChoices):
    """What the document is for. Presentation only — never an access rule."""

    INTAKE = "intake", _("Intake paperwork")
    HOMEWORK = "homework", _("Homework or worksheet")
    HANDOUT = "handout", _("Handout or reading")
    CORRESPONDENCE = "correspondence", _("Correspondence")
    OTHER = "other", _("Other")


class ScanStatus(models.TextChoices):
    CLEAN = "clean", _("Clean")
    INFECTED = "infected", _("Infected")
    # Local development only. Production refuses to start with scanning off; see
    # config/settings/prod.py.
    SKIPPED = "skipped", _("Not scanned")


class DocumentQuerySet(CaseScopedQuerySet, SoftDeleteQuerySet):
    case_path = "case"

    def shared(self):
        return self.filter(visibility=Visibility.CASE_SHARED)

    def scope_for_counselee(self, user):
        """Own uploads, plus what the counselor shared with the whole case.

        Note the membership condition is on a *current* membership, inherited from
        CaseScopedQuerySet: someone removed from a case stops seeing its
        documents, including ones they uploaded themselves. That is the right way
        round — the documents belong to the counseling relationship, and the
        history stays with the case for the counselor.
        """
        visible_to_me = models.Q(owner=user) | models.Q(visibility=Visibility.CASE_SHARED)
        return super().scope_for_counselee(user).filter(visible_to_me)

    def scope_for_financial_admin(self, user):
        # Nothing, ever. Overriding the case-scoped default back to none() is the
        # single most important line in this file.
        return self.none()


class DocumentManager(models.Manager.from_queryset(DocumentQuerySet)):
    def get_queryset(self):
        return super().get_queryset().alive()

    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class Document(SoftDeleteModel, TimeStampedModel):
    case = models.ForeignKey(
        "counseling.Case",
        on_delete=models.PROTECT,
        related_name="documents",
    )
    # PROTECT: an account that uploaded a document cannot be deleted, matching
    # AuditEvent.actor and Case.counselor. Accounts are deactivated, not removed.
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="documents",
        help_text=_("Who uploaded it. Not who it is about."),
    )
    # The session this was sent in for, when there is one. Null for most
    # documents and that is not a gap: intake paperwork arrives before any
    # appointment exists, a handout belongs to the case rather than to a Tuesday,
    # and nothing is ever *required* to be filed against a session.
    #
    # SET_NULL rather than CASCADE because the document outlives the appointment
    # in every sense that matters — losing the link would be a shame, losing a
    # counselee's file because a diary row went away would be indefensible.
    # Nothing in the application deletes a Booking (they are cancelled), so this
    # is a guard against a future migration rather than a live code path.
    booking = models.ForeignKey(
        "scheduling.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="documents",
        help_text=_("The appointment this was uploaded for, if it was for one."),
    )
    visibility = models.CharField(
        max_length=20,
        choices=Visibility.choices,
        default=Visibility.PRIVATE,
        help_text=_("Who on the case may see this, besides the counselor."),
    )
    kind = models.CharField(max_length=20, choices=DocumentKind.choices, default=DocumentKind.OTHER)

    title = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)

    original_filename = models.CharField(max_length=255)
    # Sniffed from the bytes at upload, never taken from the request. A
    # client-supplied content type is an instruction to the browser about how to
    # execute what we hand back.
    content_type = models.CharField(max_length=100)
    byte_size = models.BigIntegerField()
    # Of the plaintext, after any EXIF strip — so it identifies what a download
    # will produce, which is what a duplicate check or an integrity check wants.
    sha256 = models.CharField(max_length=64, db_index=True)

    storage_key = models.UUIDField(default=uuid4, unique=True, editable=False)
    wrapped_dek = models.BinaryField(editable=False)
    dek_nonce = models.BinaryField(editable=False)

    thumbnail_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    # Its own key, so a thumbnail and its source share no keystream.
    thumbnail_wrapped_dek = models.BinaryField(null=True, blank=True, editable=False)
    thumbnail_dek_nonce = models.BinaryField(null=True, blank=True, editable=False)

    scan_status = models.CharField(max_length=20, choices=ScanStatus.choices)
    scan_detail = models.CharField(max_length=200, blank=True)

    objects = DocumentManager()
    all_objects = models.Manager.from_queryset(DocumentQuerySet)()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["case", "-created_at"]),
            models.Index(fields=["owner", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(byte_size__gt=0), name="document_is_not_empty"
            ),
            # An infected file is never stored, so a row in this state would mean
            # a bug somewhere upstream. Refusing it in the database means that bug
            # cannot end with malware on the volume.
            models.CheckConstraint(
                condition=~models.Q(scan_status=ScanStatus.INFECTED),
                name="infected_documents_are_never_stored",
            ),
            models.CheckConstraint(
                condition=models.Q(thumbnail_key__isnull=True)
                | models.Q(thumbnail_wrapped_dek__isnull=False),
                name="thumbnail_has_a_key",
            ),
        ]
        permissions = []

    def __str__(self) -> str:
        return self.display_name

    def clean(self):
        if self.owner_id and self.case_id and not self.owner_is_party_to_the_case():
            raise ValidationError(
                {"owner": _("A document can only be uploaded by someone involved in the case.")}
            )
        # The link is a label, and this is what stops it being a route: a booking
        # on another case would put one case's appointment on another case's
        # document, where a page listing "documents for this session" would show
        # it to the wrong people. Not expressible as a check constraint — it spans
        # two rows — so the view resolves the booking through ``for_actor`` scoped
        # to the case as well.
        if self.booking_id and self.case_id and self.booking.case_id != self.case_id:
            raise ValidationError({"booking": _("That appointment is on a different case.")})

    def owner_is_party_to_the_case(self) -> bool:
        """Whether the uploader is the counselor or a current member.

        An admin uploading on someone's behalf is a legitimate case, so the check
        is a validation aid rather than the access rule; the access rule is
        ``documents.add_document`` in rules.py.
        """
        from apps.accounts.models import Role

        if self.owner.role in (Role.ADMIN, Role.COUNSELOR):
            return self.owner_id == self.case.counselor_id or self.owner.role == Role.ADMIN
        return self.case.members.filter(counselee_id=self.owner_id, ended_on__isnull=True).exists()

    @property
    def display_name(self) -> str:
        return self.title or self.original_filename

    def source_label_for(self, viewer) -> str:
        """Who to name as the source of this document, from ``viewer``'s side.

        Staff see the uploader, because provenance is the record. A counselee sees
        "You" or their counselor's name and never the name of another counselee —
        not even for a document the counselor shared with the whole case. Sharing
        is a decision about a file; it is not a decision to tell one counselee what
        the other has been handing in, and on a family case that difference is the
        whole point.
        """
        from apps.accounts.models import Role

        if self.owner_id == viewer.pk:
            return _("You")
        if self.owner_id == self.case.counselor_id:
            return self.owner.full_name
        if viewer.role == Role.COUNSELEE:
            return _("Shared with the case")
        return self.owner.full_name

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def has_thumbnail(self) -> bool:
        return self.thumbnail_key is not None

    @property
    def is_viewable_in_a_browser(self) -> bool:
        """Whether a page should offer to show this rather than hand it over.

        A hint for a template, and nothing more. ``documents.views.preview`` checks
        the allowlist again against the actual plaintext, so a content type that is
        wrong in the row produces a 404 rather than something dangerous rendered
        inline. The allowlist itself lives in apps/core/downloads.py, beside the
        headers that make serving it inline safe.
        """
        from apps.core.downloads import INLINE_TYPES

        return self.content_type in INLINE_TYPES

    @property
    def is_shared_with_the_case(self) -> bool:
        return self.visibility == Visibility.CASE_SHARED
