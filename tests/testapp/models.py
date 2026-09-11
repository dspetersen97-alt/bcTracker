"""
Concrete models that exist only to test the abstract bases in apps/core.

Soft delete guards counseling records from irrecoverable loss, so it deserves
tests against a real table rather than assertions about class attributes. This
app is installed only by config/settings/test.py.

It carries a migration rather than relying on the test runner's run_syncdb:
unmigrated apps are synced *before* migrations are applied, so Widget's foreign
key to the user table would be created before that table existed.
"""

from django.db import models

from apps.core.models import SoftDeleteModel, TimeStampedModel


class Widget(SoftDeleteModel, TimeStampedModel):
    """A stand-in for any soft-deletable, timestamped record."""

    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"

    def __str__(self) -> str:
        return self.name
