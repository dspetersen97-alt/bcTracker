"""
Invoice numbers.

An invoice number is a reference a counselee quotes on a check and an office looks
up when the bank statement arrives, so it has to be short, readable, and unique — in
that order.

**It is the invoice's public id with a prefix.** ``BC-4820193756``, where the digits
are the same ten the invoice's URL uses. One identifier rather than two, because two
would mean a counselee reading ``BC-4820193756`` on a bill and ``/invoices/7391028465/``
in their browser, with nothing to tell them those are the same document — and an
office trying to find an invoice from a payment reference would have to know which of
the two numbers the payer copied.

**No longer from a Postgres sequence.** It used to be ``nextval`` on
``billing_invoice_number_seq``, which solved a real problem — ``MAX(number) + 1`` is a
read-then-write race, and two staff raising an invoice in the same second would both
try to write ``BC-000042``. A random ten-digit id has the same property for a
different reason: it is not derived from any other row, so there is nothing to race
on. The sequence is left in place by migration 0002 rather than dropped, because
invoices already issued keep the number they were issued with and a ministry looking
at ``BC-000041`` in its records should be able to see where the run stopped.

**A sequential invoice number was also a disclosure.** ``BC-000006`` on the sixth
invoice a ministry ever raised tells the recipient how new the ministry is and how
few people it bills. That is the same argument ``apps/core/ids.py`` makes about URLs,
and it applies with more force here, because an invoice is the one document in this
application that routinely leaves the building — forwarded to a spouse, handed to a
bookkeeper, attached to an expense claim.

The prefix comes from ``settings.BILLING_INVOICE_PREFIX`` so a ministry can make the
reference look like theirs. Changing it later is safe: old invoices keep the number
they were issued with, because ``Invoice.number`` is stored rather than computed.
"""

from django.conf import settings

#: Created by migration 0002 and no longer read by this module. Named here so the
#: sequence that holds the old run has one place that says what it was for.
SEQUENCE_NAME = "billing_invoice_number_seq"


def invoice_prefix() -> str:
    """The ministry's own prefix, or ``BC``."""
    return (settings.BILLING_INVOICE_PREFIX or "BC").strip()


def invoice_number_for(public_id: str) -> str:
    """The printed reference for an invoice with this public id, e.g. ``BC-4820193756``.

    Takes the id rather than the invoice so that ``Invoice.save`` can call it before
    the row exists, which is the only moment a number may be assigned: the field is
    unique and an invoice that changed its reference after being sent out would be
    unmatchable against the payment that quotes the old one.
    """
    return f"{invoice_prefix()}-{public_id}"
