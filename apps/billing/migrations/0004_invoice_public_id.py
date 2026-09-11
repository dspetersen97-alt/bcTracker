"""
Gives fees, sessions, invoices, lines and payments the public ids that appear in their URLs.

A public id is ten random digits, and it is the only identifier any of them is shown
outside the database — see ``apps/core/ids.py`` for why a sequential primary key in
a URL is a disclosure worth removing.

Three operations per model rather than one, because a unique non-null column cannot
be added to a table that already has rows: add it nullable, give every existing row
a value, then tighten it. A deployment with no data runs all three as no-ops on an
empty table, so the same migration is correct on a fresh install and on the one that
has been in use.

Not reversible in the backfill step. Going backwards drops the column, which throws
the ids away, and reversing "assign an id" to "assign nothing" is what ``noop`` says.
"""

from functools import partial

from django.db import migrations, models

from apps.core.ids import backfill_public_ids


def _backfill(app_label, model_name, apps, schema_editor):
    backfill_public_ids(apps, app_label, model_name)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0003_sessionrecord_needs_pricing"),
    ]

    operations = [
        migrations.AddField(
            model_name="fee",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, null=True, verbose_name="reference"
            ),
        ),
        migrations.RunPython(
            partial(_backfill, "billing", "fee"),
            migrations.RunPython.noop,
            elidable=False,
        ),
        migrations.AlterField(
            model_name="fee",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, unique=True, verbose_name="reference"
            ),
        ),
        migrations.AddField(
            model_name="sessionrecord",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, null=True, verbose_name="reference"
            ),
        ),
        migrations.RunPython(
            partial(_backfill, "billing", "sessionrecord"),
            migrations.RunPython.noop,
            elidable=False,
        ),
        migrations.AlterField(
            model_name="sessionrecord",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, unique=True, verbose_name="reference"
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, null=True, verbose_name="reference"
            ),
        ),
        migrations.RunPython(
            partial(_backfill, "billing", "invoice"),
            migrations.RunPython.noop,
            elidable=False,
        ),
        migrations.AlterField(
            model_name="invoice",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, unique=True, verbose_name="reference"
            ),
        ),
        migrations.AddField(
            model_name="invoicelineitem",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, null=True, verbose_name="reference"
            ),
        ),
        migrations.RunPython(
            partial(_backfill, "billing", "invoicelineitem"),
            migrations.RunPython.noop,
            elidable=False,
        ),
        migrations.AlterField(
            model_name="invoicelineitem",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, unique=True, verbose_name="reference"
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, null=True, verbose_name="reference"
            ),
        ),
        migrations.RunPython(
            partial(_backfill, "billing", "payment"),
            migrations.RunPython.noop,
            elidable=False,
        ),
        migrations.AlterField(
            model_name="payment",
            name="public_id",
            field=models.CharField(
                editable=False, max_length=10, unique=True, verbose_name="reference"
            ),
        ),
    ]
