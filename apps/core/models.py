"""
Abstract base models shared across apps, and the deployment's own settings row.

Soft delete is the default for anything describing a counselee. Two reasons:
accidental deletion of counseling history is unrecoverable and worse than
clutter, and a future retention policy needs the deletion timestamp to decide
what is eligible for a real purge.

``MailSettings`` at the bottom is the one concrete model here. It describes the
deployment rather than a person, which is why it lives in ``core`` and not in an
app about counseling.
"""

from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.ids import PUBLIC_ID_LENGTH, unique_public_id


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PublicIdModel(models.Model):
    """Gives a model an identifier that is safe to put in a URL or an email.

    The primary key stays what it was — a ``bigint`` from a sequence, which is what
    foreign keys should be. This adds a second identifier, ten random digits, and
    that is the only one the outside world is shown. ``apps/core/ids.py`` explains
    why at length; the short version is that a sequential id in a link publishes
    both a row count and the id of every neighbouring row.

    Filled in on first save rather than by a field ``default``. A callable default
    would produce a value without consulting the table, so the uniqueness of the
    column would rest entirely on the constraint — and the one in a few billion
    time two values collided, the actor would be shown a database error in the
    middle of booking a session. ``unique_public_id`` checks first, so the
    constraint is a backstop rather than the mechanism.

    Concrete subclasses need a migration that adds the column, backfills the rows
    that already exist, and only then makes it unique and non-null; adding a unique
    non-null column to a populated table in one step cannot work. Every app that
    uses this has one, named ``*_public_id``.
    """

    public_id = models.CharField(
        max_length=PUBLIC_ID_LENGTH,
        unique=True,
        editable=False,
        verbose_name=_("reference"),
    )

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self.public_id:
            self.ensure_public_id()
            # A caller that named the fields it was updating cannot have meant to
            # skip this one, because a row without a public id has no URL and is
            # unreachable. Widening update_fields is safer than writing a row whose
            # new column stays empty.
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = [*update_fields, "public_id"]
        return super().save(*args, **kwargs)

    def ensure_public_id(self) -> str:
        """This row's public id, assigning one if it does not have it yet.

        Separate from ``save`` because one model needs the value *before* the insert
        for a reason of its own: an invoice's printed reference is its public id with
        a prefix, so ``Invoice.save`` asks for the id, formats the number from it, and
        then lets the ordinary save path run. Without this it would either have to
        duplicate the assignment or carry a second unrelated random number.
        """
        if not self.public_id:
            self.public_id = unique_public_id(type(self))
        return self.public_id


class SoftDeleteQuerySet(models.QuerySet):
    """QuerySet that can filter on, or apply, soft deletion."""

    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        return self.filter(deleted_at__isnull=False)

    def delete(self):
        """Soft-delete in bulk.

        Overriding this is deliberate: a queryset-level ``.delete()`` would
        otherwise bypass soft deletion entirely and destroy counseling records.
        Use ``hard_delete()`` when a real purge is intended.
        """
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        return super().delete()


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager that hides soft-deleted rows.

    Models using this also expose ``all_objects`` for the rare case that needs
    to see deleted rows (admin recovery, retention jobs).
    """

    def get_queryset(self):
        return super().get_queryset().alive()


class SoftDeleteModel(models.Model):
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    # Order matters: the first manager declared becomes _default_manager, which
    # is what related descriptors and generic code use. Hiding deleted rows must
    # be the default so forgetting to filter fails closed.
    objects = SoftDeleteManager()
    all_objects = models.Manager.from_queryset(SoftDeleteQuerySet)()

    class Meta:
        abstract = True

    def soft_delete(self, *, by=None):
        self.deleted_at = timezone.now()
        self.deleted_by = by
        fields = ["deleted_at", "deleted_by"]
        # Only concrete subclasses that also inherit TimeStampedModel have this,
        # and auto_now is skipped unless the field is named in update_fields.
        if any(f.name == "updated_at" for f in self._meta.concrete_fields):
            fields.append("updated_at")
        self.save(update_fields=fields)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class MailSettings(TimeStampedModel):
    """How this deployment sends mail. Exactly one row.

    Two problems this solves, both of which bit a real install:

      * the mailbox password used to live in ``.env`` in the clear, next to the
        database password, on a host whose whole point is that a stolen copy of
        the data is useless without a key held elsewhere. Here it is sealed with
        the same envelope scheme as documents and Google refresh tokens — a
        per-row DEK wrapped under ``BCTRACKER_MASTER_KEY``, so a database dump
        yields the host and the username and not the secret.
      * nobody could fix mail without editing a file on the host and restarting
        the container, which meant the first administrator of a new deployment
        could not invite anybody. This row is read on every send, so a correction
        made in the web UI takes effect on the next email.

    The sealing itself is in ``apps/core/mail.py`` rather than on the model, the
    way ``apps/scheduling/google/credentials.py`` keeps it out of
    ``GoogleCredential``: a model that can decrypt its own secret is a model that
    decrypts it in a template by accident.
    """

    #: There is one deployment, so there is one row. Pinned rather than "the
    #: latest row wins", so a second save can never leave two half-configured
    #: configurations where a reader has to guess which is live.
    SINGLETON_PK = 1

    host = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("For example smtp.gmail.com, or smtp.zoho.com."),
    )
    port = models.PositiveIntegerField(default=587)
    use_tls = models.BooleanField(
        default=True,
        help_text=_("STARTTLS, usually on port 587."),
    )
    use_ssl = models.BooleanField(
        default=False,
        help_text=_("Implicit TLS, usually on port 465. Offered instead of STARTTLS."),
    )
    username = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("The mailbox that sends. Usually the full address."),
    )
    from_email = models.EmailField(
        blank=True,
        help_text=_("What recipients see in the From line. Usually the same address."),
    )

    # Sealed, and only ever written through apps.core.mail.set_password.
    storage_key = models.UUIDField(default=uuid4, unique=True, editable=False)
    wrapped_dek = models.BinaryField(null=True, blank=True, editable=False)
    dek_nonce = models.BinaryField(null=True, blank=True, editable=False)
    password_sealed = models.BinaryField(null=True, blank=True, editable=False)

    # The record of the last "send a test message" attempt. Kept because Workspace
    # SMTP reports nothing back after a message is accepted, so the one moment we
    # can honestly say mail works is the moment somebody tested it.
    last_tested_at = models.DateTimeField(null=True, blank=True)
    last_test_error = models.CharField(max_length=300, blank=True)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("mail settings")
        verbose_name_plural = _("mail settings")
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="mail_settings_is_a_singleton"),
            # Exactly one transport security, for two separate reasons. Django's
            # SMTP backend raises ValueError when both are set, which would turn
            # every send into a server error rather than a form error. And
            # neither one set means the mailbox password crosses the network in
            # the clear, which this deployment does not do — the environment
            # path derives EMAIL_USE_TLS the same way, so there is no route to
            # plaintext SMTP from either side.
            models.CheckConstraint(
                condition=models.Q(use_tls=True, use_ssl=False)
                | models.Q(use_tls=False, use_ssl=True),
                name="mail_settings_one_transport_security",
            ),
        ]

    def __str__(self) -> str:
        return f"mail via {self.host or 'unconfigured'}"

    def save(self, *args, **kwargs):
        # Not a hint: the check constraint above refuses anything else, and
        # forcing it here means no caller has to remember.
        self.pk = self.SINGLETON_PK
        return super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The single row, created empty on first use."""
        row, _created = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return row

    @property
    def has_password(self) -> bool:
        return bool(bytes(self.password_sealed or b""))
