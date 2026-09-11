"""
One reminder for an overdue invoice, run by the cron sidecar.

**Once per invoice, and then never again**, which is a decision rather than an
unfinished dunning sequence. This is a counseling ministry: somebody who has not paid
is either busy or cannot afford it, and the second case is a pastoral conversation
rather than an escalating series of emails from a computer. The office sees every
outstanding balance on the billing page and can pick up the phone.

Safe to run more often than daily: ``services.send_invoice_reminder`` claims each
invoice with a conditional UPDATE before sending, so two overlapping runs cannot email
the same counselee twice about the same bill.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing import services


class Command(BaseCommand):
    help = "Email a single reminder for each overdue invoice that has not had one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be sent without sending or marking anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        due = services.due_invoice_reminders()
        sent = 0
        for invoice in due:
            if dry_run:
                self.stdout.write(
                    f"would remind {invoice.number}: {invoice.display_balance()} "
                    f"due {invoice.due_on}"
                )
                continue
            if services.send_invoice_reminder(invoice, now=now):
                sent += 1

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"{due.count()} reminder(s) would be sent."))
        else:
            self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent."))
