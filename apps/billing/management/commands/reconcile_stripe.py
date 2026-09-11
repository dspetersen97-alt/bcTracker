"""
Nightly check that what Stripe took and what this ministry recorded agree.

The webhook is the primary path and this is the safety net, and a payment system needs
both. A webhook can be missed for reasons entirely outside this application: the
container was restarting during Stripe's retry window, the endpoint's URL changed, the
signing secret was rolled and the old deliveries were refused, TLS failed for an hour.
Every one of those ends the same way — a counselee has paid and their invoice still
says they owe money. They will be reminded about it, and they will be quite right to
be annoyed.

So this asks the question from the other direction: **for every invoice that still
shows a balance and once sent somebody to Stripe, does Stripe think it was paid?** If
it does, the payment is recorded here, exactly as the webhook would have recorded it.
``services.apply_stripe_payment`` is idempotent on the PaymentIntent, so a webhook that
arrives after this run — or this run after the webhook — cannot double-post.

It also reports two things it will not fix, because they need a person:

  * ``StripeEvent`` rows whose outcome was ``failed``. The commonest cause is a paid
    Checkout Session naming an invoice this database does not hold, which is money in
    Stripe's account that the ministry's books know nothing about.
  * Invoices whose recorded payments exceed their total. Usually a card payment landing
    the same day as a check for the same bill. Harmless, but somebody owes a refund.

Read-only against everything except payments it is fixing, and it never sends email:
receipt or not, a counselee who paid a week ago does not need a message at 3am.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.billing.models import (
    PAYABLE_STATUSES,
    Invoice,
    Payment,
    StripeEvent,
    StripeEventOutcome,
)
from apps.billing.services import apply_stripe_payment
from apps.billing.stripe import client
from apps.billing.stripe.errors import StripeError

#: How far back to look. An invoice untouched for three months whose Checkout Session
#: was never completed is not going to be, and Stripe expires the sessions anyway.
DEFAULT_DAYS = 90


class Command(BaseCommand):
    help = "Check unpaid invoices against Stripe and record any payment the webhook missed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_DAYS,
            help="Only look at invoices issued within this many days.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report differences without recording anything.",
        )

    def handle(self, *args, **options):
        if not client.is_configured():
            # Not an error. A ministry taking cash and checks runs this way on
            # purpose, and the cron line is the same either way.
            self.stdout.write("Stripe is not configured; nothing to reconcile.")
            return

        days = options["days"]
        dry_run = options["dry_run"]
        since = timezone.now().date() - timedelta(days=days)

        recovered, checked, problems = self._reconcile_payments(since, dry_run)
        problems += self._report_failed_events()
        problems += self._report_overpayments()

        record(
            AuditVerb.STRIPE_RECONCILED,
            actor=None,
            checked=checked,
            recovered=recovered,
            problems=problems,
            dry_run=dry_run,
        )

        summary = f"{checked} invoice(s) checked, {recovered} payment(s) recovered"
        if problems:
            # Written to stderr as a warning so the cron sidecar's output makes it
            # obvious a person is needed, without failing the run — there is nothing
            # to retry.
            self.stderr.write(self.style.WARNING(f"{summary}, {problems} needing attention."))
        else:
            self.stdout.write(self.style.SUCCESS(summary + "."))

    def _reconcile_payments(self, since, dry_run):
        """Ask Stripe about every unpaid invoice that once started a checkout."""
        candidates = (
            Invoice.objects.filter(
                status__in=PAYABLE_STATUSES,
                issued_on__gte=since,
            )
            .exclude(stripe_checkout_session_id="")
            .select_related("counselee")
        )

        recovered = 0
        checked = 0
        problems = 0
        for invoice in candidates:
            if invoice.balance_cents <= 0:
                continue
            checked += 1
            try:
                session = client.retrieve_checkout_session(invoice.stripe_checkout_session_id)
            except StripeError as exc:
                self.stderr.write(f"{invoice.number}: could not read Stripe ({exc})")
                problems += 1
                continue

            if session.get("payment_status") != "paid":
                continue

            amount = session.get("amount_total") or 0
            intent = session.get("payment_intent") or ""
            if isinstance(intent, dict):
                intent = intent.get("id", "")

            self.stdout.write(
                f"{invoice.number}: Stripe says paid ({amount}) but the balance is "
                f"{invoice.balance_cents}"
            )
            if dry_run:
                continue

            # Asked before acting, so the count means what it says: recovered is the
            # number of payments the webhook genuinely missed. apply_stripe_payment
            # returns the existing row rather than a second one when the webhook did
            # arrive, which is what makes running this alongside it safe.
            already_recorded = (
                bool(intent) and Payment.objects.filter(stripe_payment_intent_id=intent).exists()
            )
            apply_stripe_payment(
                invoice,
                payment_intent_id=intent,
                amount_cents=amount,
                reference=f"Stripe {session.get('id', '')} (reconciled)",
            )
            if not already_recorded:
                recovered += 1

        return recovered, checked, problems

    def _report_failed_events(self) -> int:
        """Webhook deliveries that verified and then could not be handled."""
        failed = StripeEvent.objects.filter(outcome=StripeEventOutcome.FAILED)
        for event in failed:
            self.stderr.write(
                f"Stripe event {event.stripe_event_id} ({event.event_type}) failed: {event.detail}"
            )
        return failed.count()

    def _report_overpayments(self) -> int:
        """Invoices with more money against them than they asked for."""
        overpaid = [
            invoice
            for invoice in Invoice.objects.exclude(amount_paid_cents=0)
            if invoice.balance_cents < 0
        ]
        for invoice in overpaid:
            self.stderr.write(
                f"{invoice.number} is overpaid by {invoice.display_balance()} — a refund is owed."
            )
        return len(overpaid)
