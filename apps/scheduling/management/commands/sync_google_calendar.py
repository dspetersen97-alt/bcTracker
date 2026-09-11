"""
Push appointments to Google, run by the cron sidecar every 15 minutes.

The cron tick is the *safety net*, not the mechanism. Booking, confirming,
cancelling, and rescheduling each push immediately; this exists for the ones that
did not land — Google was down, the container was restarting mid-request, a token
expired at the wrong moment. Fifteen minutes is chosen against that: long enough
not to matter for a system that already pushed synchronously, short enough that a
counselor who fixed their connection sees their week fill in before they wonder
whether it worked.

Idempotent by construction. ``google_synced_at`` is compared against
``updated_at``, so a booking already pushed is not pushed again, and running this
by hand at any moment is safe:

    docker compose exec web python manage.py sync_google_calendar --dry-run

Exits 0 even when individual pushes failed. A cron job that exits non-zero on a
transient Google error mails an administrator about something that will fix itself
on the next tick, and an alert that cries wolf is worse than no alert. A genuinely
dead grant is surfaced where it can be acted on — on the counselor's own settings
page — and counted in the output here.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.accounts.models import Role, User
from apps.scheduling.google import sync


class Command(BaseCommand):
    help = "Send bookings to the Google calendars of counselors who have connected one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--counselor",
            help="Limit to one counselor, by email address. For diagnosing one connection.",
        )
        parser.add_argument(
            "--days",
            type=int,
            help="How far ahead to look. Defaults to GOOGLE_SYNC_HORIZON_DAYS.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the appointments that would be sent, without contacting Google.",
        )

    def handle(self, *args, **options):
        if not settings.GOOGLE_CALENDAR_ENABLED:
            # Not a failure. The integration is optional and a ministry that has not
            # set it up should see a plain sentence rather than a traceback in a log
            # they only read when something is wrong.
            self.stdout.write("Google Calendar is switched off. Nothing to do.")
            return

        counselors = self._counselors(options.get("counselor"))
        if counselors is None:
            return

        if options["dry_run"]:
            self._report(counselors, days=options.get("days"))
            return

        if counselors is Ellipsis:
            totals = sync.sync_all(horizon_days=options.get("days"))
            self.stdout.write(
                self.style.SUCCESS(
                    "{counselors} calendar(s): {pushed} appointment(s) sent, "
                    "{failed} failed, {disconnected} disconnected.".format(**totals)
                )
            )
            return

        for counselor in counselors:
            tally = sync.sync_counselor(counselor, horizon_days=options.get("days"))
            if tally["skipped"]:
                self.stdout.write(f"{counselor.email}: no usable Google connection.")
                continue
            line = f"{counselor.email}: {tally['pushed']} sent, {tally['failed']} failed"
            if tally.get("authorization_lost"):
                self.stdout.write(self.style.ERROR(f"{line} — authorization lost, must reconnect."))
            else:
                self.stdout.write(self.style.SUCCESS(line))

    def _counselors(self, email):
        """The counselors to act on: ``Ellipsis`` for all of them, or a list, or None.

        ``Ellipsis`` rather than ``None`` for "everyone", because ``None`` is what
        this returns when the argument named somebody who does not exist and the two
        must not be confused — silently syncing every calendar in the ministry
        because of a typo in an email address is the wrong way to fail.
        """
        if not email:
            return Ellipsis
        counselor = User.objects.filter(email__iexact=email, role=Role.COUNSELOR).first()
        if counselor is None:
            self.stderr.write(self.style.ERROR(f"No counselor with the address {email}."))
            return None
        return [counselor]

    def _report(self, counselors, *, days):
        """What would be sent. Times only — never a counselee's name.

        The output of a cron job ends up in a container log, which is read by
        whoever runs the server and is not part of the access model. So this prints
        what a Google event would carry and nothing more.
        """
        from apps.scheduling.models import GoogleCredential

        if counselors is Ellipsis:
            credentials = GoogleCredential.objects.filter(revoked_at__isnull=True).select_related(
                "counselor"
            )
            counselors = [credential.counselor for credential in credentials]

        total = 0
        for counselor in counselors:
            pending = sync.bookings_needing_push(counselor, horizon_days=days)
            for booking in pending:
                self.stdout.write(
                    f"{counselor.email}: booking {booking.pk} "
                    f"{booking.starts_at:%Y-%m-%d %H:%M %Z} ({booking.status})"
                )
                total += 1
        self.stdout.write(self.style.SUCCESS(f"{total} appointment(s) would be sent."))
