"""
Appointment reminders, run by the cron sidecar.

A management command rather than a task queue. There is no broker in v1 on
purpose: a ministry sends a few dozen reminders a day, and Celery would add a
service to run, monitor, and restore from backup in exchange for nothing this
workload needs.

Safe to run more often than the window: ``services.send_reminder`` claims each
appointment with a conditional UPDATE before sending, so two overlapping runs
cannot remind the same counselee twice. Running it hourly with a 24-hour window is
the intended configuration — an appointment booked at short notice still gets a
reminder, and one that was rescheduled gets a fresh one.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.scheduling import services


class Command(BaseCommand):
    help = "Email reminders for confirmed appointments starting soon."

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24,
            help="Remind about appointments starting within this many hours.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be sent without sending or marking anything.",
        )

    def handle(self, *args, **options):
        hours = options["hours"]
        dry_run = options["dry_run"]
        now = timezone.now()

        due = services.due_reminders(within_hours=hours, now=now)
        sent = 0
        for booking in due:
            when = booking.starts_at.astimezone(booking.counselee.zoneinfo)
            if dry_run:
                self.stdout.write(f"would remind booking {booking.pk} for {when:%Y-%m-%d %H:%M %Z}")
                continue
            if services.send_reminder(booking, now=now):
                sent += 1

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"{due.count()} reminder(s) would be sent."))
        else:
            self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent."))
