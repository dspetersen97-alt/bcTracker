"""
The sequence invoice numbers are drawn from.

Raw SQL because a Postgres sequence is not a Django model, and it is a sequence
rather than ``MAX(number) + 1`` because the obvious implementation is a read-then-write
race — see apps/billing/numbering.py, which explains the trade-off it buys and the
gaps it costs.

``CREATE SEQUENCE IF NOT EXISTS`` and a matching drop, so the migration is safe to
apply to a database where a previous run got half way. The reverse drops it, which
loses the counter's position: an invoice number is stored on the row, so old invoices
keep theirs, but a re-applied migration would start again at 1 and collide with them.
That is a migration nobody should reverse on a database that has issued invoices, and
saying so here is more use than a comment nobody reads in a runbook.
"""

from django.db import migrations

from apps.billing.numbering import SEQUENCE_NAME


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql=f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE_NAME} START WITH 1 INCREMENT BY 1;",
            reverse_sql=f"DROP SEQUENCE IF EXISTS {SEQUENCE_NAME};",
        ),
    ]
