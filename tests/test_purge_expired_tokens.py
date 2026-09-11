"""
The retention purge.

A command that deletes rows every thirty minutes deserves tests that are mostly
about what it does *not* delete. Three tables, one rule each:

  * a login link that is still usable must survive, or a counselee following the
    link in their email lands on an error;
  * a session that has not expired must survive, or everybody is signed out twice
    an hour;
  * a failed-login record that can still lock somebody out must survive, or the
    purge becomes a way to reset the throttle and keep guessing.

And the audit trail is never touched by any of it: it is the record of who did
what, and these three tables are working state.
"""

import datetime as dt

import pytest
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import LoginToken, TokenPurpose
from apps.audit.models import AuditEvent, AuditVerb

pytestmark = pytest.mark.django_db


def token_for(user, *, expires_in_days=1, purpose=TokenPurpose.MAGIC_LINK, consumed=False):
    """A login link with its expiry moved to an arbitrary point in time.

    Issued through the real ``LoginToken.issue`` and then rewritten, so the row is
    shaped exactly like one the login flow produces.
    """
    token, _ = LoginToken.issue(user=user, purpose=purpose, ttl_seconds=900)
    LoginToken.objects.filter(pk=token.pk).update(
        expires_at=timezone.now() + dt.timedelta(days=expires_in_days),
        consumed_at=timezone.now() if consumed else None,
    )
    token.refresh_from_db()
    return token


def session_expiring(days: int) -> Session:
    return Session.objects.create(
        session_key=f"key{abs(days)}{'past' if days < 0 else 'future'}",
        session_data="",
        expire_date=timezone.now() + dt.timedelta(days=days),
    )


def attempt_aged(minutes: int):
    """A django-axes failed-login record, ``minutes`` old."""
    from axes.models import AccessAttempt

    attempt = AccessAttempt.objects.create(
        username=f"someone-{minutes}@example.org",
        ip_address="198.51.100.7",
        user_agent="",
        path_info="/login/",
        failures_since_start=1,
    )
    AccessAttempt.objects.filter(pk=attempt.pk).update(
        attempt_time=timezone.now() - dt.timedelta(minutes=minutes)
    )
    return attempt


class TestLoginLinks:
    def test_a_link_expired_longer_ago_than_the_retention_window_is_deleted(
        self, counselee, settings
    ):
        settings.LOGIN_TOKEN_RETENTION_DAYS = 30
        stale = token_for(counselee, expires_in_days=-45)

        call_command("purge_expired_tokens")

        assert not LoginToken.objects.filter(pk=stale.pk).exists()

    def test_a_usable_link_is_kept(self, counselee):
        """The one outcome a counselee would notice. Guaranteed by the cutoff being
        in the past rather than by a condition on this row, which is why it is worth
        asserting rather than reading."""
        live = token_for(counselee, expires_in_days=1)

        call_command("purge_expired_tokens")

        assert LoginToken.objects.get(pk=live.pk).is_usable

    def test_a_recently_expired_link_is_kept(self, counselee, settings):
        """Kept for the retention window because the row still answers "where was
        this link used from", which is the question asked when a counselee says
        somebody else read their mail."""
        settings.LOGIN_TOKEN_RETENTION_DAYS = 30
        yesterday = token_for(counselee, expires_in_days=-1)

        call_command("purge_expired_tokens")

        assert LoginToken.objects.filter(pk=yesterday.pk).exists()

    def test_a_spent_link_is_kept_until_the_window_passes_too(self, counselee, settings):
        """A consumed link is dead immediately, but it is the most interesting row in
        this table during an investigation, so it goes on the same clock."""
        settings.LOGIN_TOKEN_RETENTION_DAYS = 30
        spent = token_for(counselee, expires_in_days=-1, consumed=True)

        call_command("purge_expired_tokens")

        assert LoginToken.objects.filter(pk=spent.pk).exists()

    def test_the_window_can_be_overridden_on_the_command_line(self, counselee, settings):
        settings.LOGIN_TOKEN_RETENTION_DAYS = 30
        recent = token_for(counselee, expires_in_days=-2)

        call_command("purge_expired_tokens", "--token-retention-days", "1")

        assert not LoginToken.objects.filter(pk=recent.pk).exists()

    def test_invitations_and_password_resets_are_covered_too(self, counselee, settings):
        """All three purposes share one table and one expiry rule; a purge that only
        handled magic links would leave invitation tokens forever."""
        settings.LOGIN_TOKEN_RETENTION_DAYS = 1
        for purpose in TokenPurpose.values:
            token_for(counselee, expires_in_days=-10, purpose=purpose)

        call_command("purge_expired_tokens")

        assert LoginToken.objects.count() == 0


class TestSessions:
    def test_an_expired_session_row_is_deleted(self):
        """Django does not do this for us. A session past its expiry is still a row
        holding a user id and a "second factor satisfied" flag."""
        expired = session_expiring(-1)

        call_command("purge_expired_tokens")

        assert not Session.objects.filter(session_key=expired.session_key).exists()

    def test_a_live_session_is_untouched(self):
        live = session_expiring(1)

        call_command("purge_expired_tokens")

        assert Session.objects.filter(session_key=live.session_key).exists()

    def test_a_signed_in_counselor_stays_signed_in(self, counselor, sign_in):
        """The end-to-end version of the test above, because getting this wrong would
        sign every counselor out twice an hour and would look like a session-timeout
        bug rather than a purge bug."""
        from django.urls import reverse

        client = sign_in(counselor)

        call_command("purge_expired_tokens")

        # Followed, because a counselor's dashboard redirects on to their caseload.
        # What is being asserted is that it does not redirect to the login form.
        landed = client.get(reverse("counseling:dashboard"), follow=True)
        assert landed.status_code == 200
        assert "/login/" not in landed.request["PATH_INFO"]


class TestFailedLoginRecords:
    def test_a_stale_attempt_is_deleted(self, settings):
        """These rows hold an address somebody typed at the login form. Once they can
        no longer affect a lockout they are only a list of who has tried to sign in
        here."""
        settings.AXES_COOLOFF_TIME = dt.timedelta(minutes=15)
        from axes.models import AccessAttempt

        attempt_aged(minutes=60 * 48)

        call_command("purge_expired_tokens")

        assert AccessAttempt.objects.count() == 0

    def test_an_attempt_that_could_still_lock_somebody_out_is_kept(self, settings):
        """The rule that makes this safe. Deleting a countable failure would let
        somebody who has been refused start again immediately, which is the outcome
        the throttle exists to prevent — and the purge runs every thirty minutes,
        twice as often as a fifteen-minute cooloff.
        """
        settings.AXES_COOLOFF_TIME = dt.timedelta(minutes=15)
        from axes.models import AccessAttempt

        recent = attempt_aged(minutes=5)

        call_command("purge_expired_tokens")

        assert AccessAttempt.objects.filter(pk=recent.pk).exists()

    def test_an_active_lockout_survives_a_purge(self, client, counselee, settings):
        """Asserted through the login form rather than the table, because what
        matters is that the lockout still refuses the correct password."""
        from django.urls import reverse

        from tests.conftest import TEST_PASSWORD

        settings.AXES_ENABLED = True
        settings.AXES_FAILURE_LIMIT = 5
        settings.AXES_COOLOFF_TIME = dt.timedelta(minutes=15)
        for _ in range(5):
            client.post(reverse("accounts:login"), {"username": counselee.email, "password": "no"})

        call_command("purge_expired_tokens")

        refused = client.post(
            reverse("accounts:login"),
            {"username": counselee.email, "password": TEST_PASSWORD},
        )
        assert refused.status_code == 429

    def test_nothing_is_removed_when_lockouts_never_expire(self, settings):
        """With no cooloff configured a lockout lasts until somebody clears it, so no
        attempt row is safe to delete on age alone."""
        settings.AXES_COOLOFF_TIME = None
        from axes.models import AccessAttempt

        attempt_aged(minutes=60 * 24 * 365)

        call_command("purge_expired_tokens")

        assert AccessAttempt.objects.count() == 1


class TestWhatIsReportedAndRecorded:
    def test_a_dry_run_deletes_nothing(self, counselee, settings, capsys):
        settings.LOGIN_TOKEN_RETENTION_DAYS = 1
        token_for(counselee, expires_in_days=-10)
        session_expiring(-1)

        call_command("purge_expired_tokens", "--dry-run")

        assert LoginToken.objects.count() == 1
        assert Session.objects.count() == 1
        assert "would delete" in capsys.readouterr().out

    def test_a_purge_that_removed_something_is_audited_with_the_counts(self, counselee, settings):
        settings.LOGIN_TOKEN_RETENTION_DAYS = 1
        token_for(counselee, expires_in_days=-10)
        session_expiring(-1)

        call_command("purge_expired_tokens")

        event = AuditEvent.objects.filter(verb=AuditVerb.RETENTION_PURGED).get()
        assert event.metadata["login_tokens"] == 1
        assert event.metadata["sessions"] == 1
        assert event.actor is None

    def test_a_purge_with_nothing_to_do_writes_no_row(self):
        """It runs every thirty minutes. An append-only table should not fill up with
        the news that there was nothing to delete."""
        call_command("purge_expired_tokens")

        assert not AuditEvent.objects.filter(verb=AuditVerb.RETENTION_PURGED).exists()

    def test_the_audit_trail_is_never_what_gets_purged(self, counselee, settings):
        """Stated as a test because "delete old records" is exactly the instruction
        somebody would later extend to this table, and it is the one table that must
        not shrink."""
        settings.LOGIN_TOKEN_RETENTION_DAYS = 1
        token_for(counselee, expires_in_days=-400)
        AuditEvent.objects.create(verb=AuditVerb.LOGIN_SUCCEEDED, actor=counselee)
        before = AuditEvent.objects.count()

        call_command("purge_expired_tokens")

        assert AuditEvent.objects.count() >= before
