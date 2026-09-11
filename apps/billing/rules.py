"""
Object-level permissions for billing.

This is the one app in the project where ``financial_admin`` is a first-class
role rather than a role being kept out, and reading it alongside
apps/documents/rules.py is the clearest statement of what that role is: everything
here, nothing there.

Four things are worth reading before changing any of it.

**The payer can see and pay, and can do nothing else.** A counselee holds
``view_invoice`` for an invoice addressed to them and ``pay_invoice`` while it is
open. They cannot record a payment — "I paid, mark it paid" is not a permission,
it is an assertion — and they cannot see the fee schedule, a draft, or another
member of the case's bill.

**A counselor reads and never writes.** They may see the invoices on their own
cases, because a counselee will ask them about a bill and "I have no idea" is a bad
answer from the person in the room. They may not raise, issue, void, or take
payment: a counselor negotiating money with the person they are counseling is the
conflict of interest this separation exists to prevent.

**An admin can do everything billing can.** Deliberately, and unlike messaging,
where an admin reads but does not write. Money is administration; the ministry
administrator is accountable for it, and a ministry too small to have a separate
bookkeeper still has to be able to send an invoice.

**A draft is invisible to the person it is about.** Not merely uneditable by them —
a draft is the office working out what to charge, and a counselee seeing an amount
that is still being decided would be told something the ministry has not decided to
say. ``view_invoice`` therefore requires the invoice to have been issued before the
payer can see it, which is the one place in this app where a permission depends on
state rather than only on who is asking.
"""

import rules

from apps.accounts.rules import is_admin, is_counselee, is_financial_admin
from apps.counseling.rules import is_case_counselor, is_case_member

#: Everyone who runs the ministry's money. Named because it is written eleven times
#: below and the two roles must not drift apart in a way nobody notices.
manages_billing = is_admin | is_financial_admin


@rules.predicate
def is_the_payer(user, invoice):
    if invoice is None:
        return False
    return invoice.counselee_id == user.pk


@rules.predicate
def is_counselor_on_the_invoices_case(user, invoice):
    if invoice is None:
        return False
    return is_case_counselor(user, invoice.case)


@rules.predicate
def invoice_has_been_issued(user, invoice):
    """Anything but a draft.

    A voided invoice counts as issued: the payer was told about it, and being able
    to see that it was withdrawn is the point of voiding rather than deleting.
    """
    if invoice is None:
        return False
    return not invoice.is_draft


@rules.predicate
def invoice_is_a_draft(user, invoice):
    if invoice is None:
        return False
    return invoice.is_draft


@rules.predicate
def invoice_can_be_voided(user, invoice):
    """Not already void, and no money against it.

    A paid invoice is not voided — that would leave the ministry holding money
    against nothing. It is refunded, which happens at the payment layer, and the
    invoice keeps saying what it said.
    """
    from apps.billing.models import InvoiceStatus

    if invoice is None:
        return False
    return invoice.status != InvoiceStatus.VOID and invoice.amount_paid_cents == 0


@rules.predicate
def invoice_can_take_a_payment(user, invoice):
    from apps.billing.models import PAYABLE_STATUSES

    if invoice is None:
        return False
    return invoice.status in PAYABLE_STATUSES


@rules.predicate
def invoice_is_payable_by_card(user, invoice):
    """Open, owing something, and Stripe actually configured.

    The Stripe check is in the predicate rather than only in the view so that the
    template asking "should I show a Pay button" gets the same answer as the route
    behind it. A ministry running without Stripe sees no button and no dead end.
    """
    from django.conf import settings

    if invoice is None:
        return False
    return settings.STRIPE_ENABLED and invoice.is_payable


@rules.predicate
def session_is_not_yet_invoiced(user, session):
    """A session's fee can be corrected right up until it is on a live invoice.

    After that it is frozen, because the number on it is the number a counselee has
    been told. The correction path then is to void the invoice and raise another.
    """
    if session is None:
        return False
    return not session.is_invoiced


# --- indexes and lists ----------------------------------------------------

# The ministry's billing overview: what is outstanding, what is waiting to be
# invoiced. No object, so this is about the role, and the roles that are missing are
# the statement — a counselor has no ministry-wide view of money, and a counselee's
# own bills are a different page.
rules.add_perm("billing.view_billing_index", manages_billing)

# A counselee's own invoices. Only a counselee: staff reach the same rows through a
# case or through the billing index, and a page called "my invoices" that a staff
# member could open would have to mean somebody else's. Staff are sent to their own
# billing page rather than refused — the wrong door, not a refusal, which is how
# counseling:my_cases treats them too.
rules.add_perm("billing.view_own_invoices", is_counselee)

# The invoices on one case. Checked against a *Case*, so everyone who can read the
# case can reach the page; what differs is what is on it. A counselee sees the rows
# addressed to them and no others, which InvoiceQuerySet decides, not this.
rules.add_perm(
    "billing.view_case_invoices",
    manages_billing | is_case_counselor | is_case_member,
)

# The fee schedule. Ministry configuration; see FeeQuerySet for why a counselor is
# not on this list.
rules.add_perm("billing.view_fees", manages_billing)
rules.add_perm("billing.manage_fees", manages_billing)


# --- one invoice ----------------------------------------------------------

# Reading a bill. The payer only once it has been issued; see the module docstring.
rules.add_perm(
    "billing.view_invoice",
    manages_billing | is_counselor_on_the_invoices_case | (is_the_payer & invoice_has_been_issued),
)

# Raising one. Checked against a *Case*, since there is no invoice yet.
rules.add_perm("billing.add_invoice", manages_billing)

# Changing a draft — adding and removing lines. Only while it is a draft: an issued
# invoice is immutable, which is what makes it a statement rather than a running
# total.
rules.add_perm("billing.change_invoice", manages_billing & invoice_is_a_draft)

# Sending it. The state change that makes the amount real, so it is the same two
# roles and it can only happen once.
rules.add_perm("billing.issue_invoice", manages_billing & invoice_is_a_draft)

# Withdrawing it. See invoice_can_be_voided for why a paid invoice is excluded.
rules.add_perm("billing.void_invoice", manages_billing & invoice_can_be_voided)

# Writing it off as uncollectible. Kept apart from voiding on purpose: voiding says
# the invoice was wrong, writing off says it was right and will not be paid. A
# ministry's books need to be able to tell those two apart.
rules.add_perm("billing.write_off_invoice", manages_billing & invoice_can_take_a_payment)

# Recording money that arrived by cash, check, or transfer. The office's job, and
# emphatically not the payer's.
rules.add_perm("billing.record_payment", manages_billing & invoice_can_take_a_payment)

# Reversing a payment — a bounced check, a card refund. Same two roles, and it
# writes a new row rather than editing one; see Payment's docstring.
rules.add_perm("billing.reverse_payment", manages_billing)

# Paying. The payer alone. Not an administrator "on their behalf": a card payment
# started by staff would be somebody else's card, and taking a counselee's card
# number over the phone is the exact thing hosted Checkout exists to avoid.
rules.add_perm("billing.pay_invoice", is_the_payer & invoice_is_payable_by_card)


# --- sessions -------------------------------------------------------------

# Correcting a recorded session's fee. Needed because a session recorded before
# anybody set up the fee schedule comes out at nothing, and somebody has to be able
# to put that right. Frozen once invoiced.
rules.add_perm("billing.change_session", manages_billing & session_is_not_yet_invoiced)

# Deleting a session record is not a permission anyone holds. A session that should
# not be charged for is marked not billable, with a reason, which leaves the fact
# that it happened in place. Stated here so its absence reads as a decision.
rules.add_perm("billing.delete_session", rules.predicate(lambda user, session: False))
