"""
Invoice numbers.

An invoice number is a reference a counselee quotes on a check and an office
looks up when the bank statement arrives, so it has to be short, readable, and
unique — in that order.

**From a Postgres sequence, not from ``MAX(number) + 1``.** The obvious
implementation reads the highest number and adds one, which is a read-then-write
race: two staff members raising an invoice in the same second both read 41 and both
try to write 42, and one of them gets an ``IntegrityError`` from the unique
constraint after having filled in a form. A sequence hands out 42 and 43 without
either transaction waiting for the other. The same reasoning as the exclusion
constraint in scheduling — the database is the only place a counter can be correct
under concurrency.

**A gap in the run is not a missing invoice.** ``nextval`` is deliberately not
transactional: a number consumed by a transaction that then rolls back is gone.
That is the price of the paragraph above, and it is the right way round — a gap is
explainable, whereas two invoices numbered BC-000042 are not. If a ministry's
accountant needs an unbroken sequence, the answer is a separate register they
maintain, not a counter this application can be raced on.

The prefix comes from ``settings.BILLING_INVOICE_PREFIX`` so a ministry can make
the reference look like theirs. Changing it later is safe: the sequence keeps
counting, and old invoices keep the number they were issued with, because
``Invoice.number`` is stored rather than computed.
"""

from django.conf import settings
from django.db import connection

#: Created by migration 0002. Named here rather than in the migration alone so the
#: two cannot drift apart silently.
SEQUENCE_NAME = "billing_invoice_number_seq"

#: Zero-padded width. Six digits is 999,999 invoices — comfortably more than a
#: counseling ministry will raise, and short enough to read aloud over the phone.
WIDTH = 6


def next_invoice_number() -> str:
    """The next reference, e.g. ``BC-000042``.

    Called inside ``services.create_invoice``'s transaction. It does not take a
    lock and does not wait: see the module docstring.
    """
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT nextval('{SEQUENCE_NAME}')")  # noqa: S608 — a constant
        (value,) = cursor.fetchone()

    prefix = (settings.BILLING_INVOICE_PREFIX or "BC").strip()
    return f"{prefix}-{int(value):0{WIDTH}d}"
