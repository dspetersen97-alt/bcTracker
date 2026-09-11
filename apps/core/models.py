"""
Abstract base models shared across apps.

Soft delete is the default for anything describing a counselee. Two reasons:
accidental deletion of counseling history is unrecoverable and worse than
clutter, and a future retention policy needs the deletion timestamp to decide
what is eligible for a real purge.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


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
