"""
Billing notifications.

The rules from apps/scheduling/notify.py and apps/messaging/notify.py hold here
too: every send logs and swallows, and every link is built from
``SITE_BASE_URL`` rather than from the request, so a spoofed Host header cannot
decide where a counselee is sent to sign in.

**What is different is that these emails carry an amount.** Messaging's notification
deliberately says only "you have a message", because the content is counseling. A
bill is not: an invoice email that omitted the amount and the reference would send
the payer to sign in to find out what they had been told, and most people would not
bother — which is a worse outcome for the counselee than for the ministry, since the
first thing they hear about it would be a reminder. So the number, the amount, and
the due date go in the mail.

**What stays out is everything that would say what the money was for.** No case
label, no counselor's name, no session dates, no line items. "Invoice BC-000042 for
$340.00" read over somebody's shoulder says they owe a ministry money. The same
sentence with "3 counseling sessions with Pastor Dan" in it says something else
entirely, and email is the one thing in this system we cannot take back.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.mail import from_address

logger = logging.getLogger(__name__)


def _send(*, template, subject, recipient, context) -> None:
    try:
        send_mail(
            subject=subject,
            message=render_to_string(f"billing/email/{template}.txt", context),
            from_email=from_address(),
            recipient_list=[recipient.email],
            fail_silently=False,
        )
    except Exception:
        # Broad, as everywhere else: SMTP raises a family of errors, a DNS failure
        # raises something else again, and an invoice that has been issued is issued
        # whether or not the mail got out.
        logger.exception("Could not send %s to user %s", template, recipient.pk)
    else:
        logger.info("sent %s to user %s", template, recipient.pk)


def _context(invoice):
    """What every billing email says. One function, so they cannot drift apart."""
    return {
        "recipient": invoice.counselee,
        "number": invoice.number,
        "total": invoice.display_total(),
        "balance": invoice.display_balance(),
        "due_on": invoice.due_on,
        # The invoice page, not a payment link. Paying needs a session, and a URL
        # that took money without one would be a phishing template.
        # reverse() rather than a literal path: an invoice is addressed by its public
        # id, and a hand-built "/invoices/<pk>/" would have kept working right up to
        # the point where somebody clicked it. See apps/core/ids.py.
        "url": settings.SITE_BASE_URL
        + reverse("billing:invoice_detail", kwargs={"public_id": invoice.public_id}),
        "site_url": settings.SITE_BASE_URL,
        "can_pay_by_card": settings.STRIPE_ENABLED,
    }


def invoice_issued(invoice) -> None:
    """Tell the payer a bill has been raised. Sent once, by ``services.issue_invoice``."""
    if not invoice.counselee.is_active:
        # A deactivated account cannot sign in to see it, so the mail would be a
        # dead end. The invoice still stands and the office can chase it by post.
        logger.info(
            "Invoice %s not emailed: counselee %s is not active.",
            invoice.number,
            invoice.counselee_id,
        )
        return

    _send(
        template="invoice_issued",
        subject=f"Invoice {invoice.number}",
        recipient=invoice.counselee,
        context=_context(invoice),
    )


def invoice_reminder(invoice) -> None:
    """A single nudge on an overdue invoice. Sent by ``manage.py send_invoice_reminders``.

    Deliberately gentle and deliberately once — see the command for why this is not
    a dunning sequence. Somebody in counseling who cannot pay their bill is a
    pastoral conversation, not an escalating series of emails.
    """
    if not invoice.counselee.is_active:
        return

    _send(
        template="invoice_reminder",
        subject=f"Invoice {invoice.number} is now due",
        recipient=invoice.counselee,
        context=_context(invoice),
    )


def payment_received(payment) -> None:
    """Confirm money in, for a card payment.

    Only for card payments, and only because the payer was not in the room: they
    typed their card into a Stripe page and came back to ours, and a receipt is how
    they know the two connected. Cash handed over in person needs no email, which is
    why ``services.record_payment`` does not send one.
    """
    invoice = payment.invoice
    if not invoice.counselee.is_active:
        return

    context = _context(invoice)
    context["amount"] = payment.display_amount()
    _send(
        template="payment_received",
        subject=f"Payment received for invoice {invoice.number}",
        recipient=invoice.counselee,
        context=context,
    )
