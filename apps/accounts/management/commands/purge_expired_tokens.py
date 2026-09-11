"""
Delete credential material that has expired.

Named for what compose/cron/bctracker.cron calls it, but it covers all three
places a spent credential lingers, because they are one concern:

  * **Login links** (``LoginToken``) — magic links, invitations, password resets.
  * **Sessions** — a session row past its expiry is still a row containing a user
    id and a verified-second-factor flag, and Django does not remove them for us.
  * **Failed-login records** (django-axes ``AccessAttempt``) — kept only until
    they can no longer affect a lockout decision, because each one holds an email
    address somebody typed at this login form.

Deleting is the point, so the safety rules are stated in one place:

  * Nothing still usable is ever deleted. Every cutoff is in the past.
  * The audit trail is untouched. It is append-only and is the record of who did
    what; these three tables are working state, not history. The trail keeps the
    ``auth.magic_link.sent`` and ``auth.login.failed`` rows either way.
  * A purge that removed nothing writes no audit row. It runs every 30 minutes,
    and an append-only table should not be filled with the news that there was
    nothing to do.
"""

import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import LoginToken
from apps.audit.models import AuditVerb
from apps.audit.services import record


class Command(BaseCommand):
    help = "Delete expired login links, expired sessions, and stale login-failure records."

    def add_arguments(self, parser):
        parser.add_argument(
            "--token-retention-days",
            type=int,
            default=None,
            help=(
                "Keep expired login links this many days before deleting them "
                "(default: LOGIN_TOKEN_RETENTION_DAYS)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        retention_days = options["token_retention_days"]
        if retention_days is None:
            retention_days = settings.LOGIN_TOKEN_RETENTION_DAYS
        now = timezone.now()

        counts = {
            "login_tokens": self._purge_tokens(now, retention_days, dry_run=dry_run),
            "sessions": self._purge_sessions(now, dry_run=dry_run),
            "login_attempts": self._purge_access_attempts(now, dry_run=dry_run),
        }

        for name, count in counts.items():
            verb = "would delete" if dry_run else "deleted"
            self.stdout.write(f"{name}: {verb} {count}")

        total = sum(counts.values())
        if total and not dry_run:
            record(AuditVerb.RETENTION_PURGED, **counts)
        outcome = "found" if dry_run else "purged"
        self.stdout.write(self.style.SUCCESS(f"{total} expired record(s) {outcome}."))

    def _purge_tokens(self, now, retention_days, *, dry_run):
        """Login links whose expiry is far enough in the past.

        Keyed on ``expires_at`` rather than ``consumed_at`` so one condition covers
        both a link that was used and one that never was: a token's lifetime is
        minutes to days, so its expiry is a close proxy for when it stopped
        mattering, and it is the indexed column.
        """
        cutoff = now - dt.timedelta(days=retention_days)
        expired = LoginToken.objects.filter(expires_at__lt=cutoff)
        if dry_run:
            return expired.count()
        deleted, _ = expired.delete()
        return deleted

    def _purge_sessions(self, now, *, dry_run):
        """Session rows past their expiry.

        Sessions are stored in the database (``SESSION_ENGINE`` in
        config/settings/base.py); on a cache-backed engine this would find nothing
        and the engine would expire its own keys. Written as a queryset rather than
        a call to ``clearsessions`` so the count can be reported — a purge that
        cannot say what it removed is hard to trust.

        Worth being clear about what this is not: it does not sign anybody out.
        These rows are already dead as credentials; the idle timeout is what ends
        a live session (SESSION_COOKIE_AGE).
        """
        from django.contrib.sessions.models import Session

        expired = Session.objects.filter(expire_date__lt=now)
        if dry_run:
            return expired.count()
        deleted, _ = expired.delete()
        return deleted

    def _purge_access_attempts(self, now, *, dry_run):
        """Failed-login records that can no longer lock anybody out.

        The cutoff is the cooloff window plus a day. The margin matters: these rows
        are what django-axes counts, so deleting one that is still inside its window
        would end a lockout early — which is exactly the outcome somebody guessing
        at a counselor's password would want. A day of slack costs nothing and
        makes that impossible.
        """
        from axes.models import AccessAttempt

        cooloff = getattr(settings, "AXES_COOLOFF_TIME", None)
        if cooloff is None:
            # No cooloff configured means a lockout lasts until it is cleared by
            # hand, so no attempt row is ever safe to remove on age alone.
            return 0
        window = cooloff if isinstance(cooloff, dt.timedelta) else dt.timedelta(hours=cooloff)
        cutoff = now - window - dt.timedelta(days=1)

        stale = AccessAttempt.objects.filter(attempt_time__lt=cutoff)
        if dry_run:
            return stale.count()
        deleted, _ = stale.delete()
        return deleted
