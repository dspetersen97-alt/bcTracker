"""
Login throttling.

The login form is the one page on this system reachable by anybody on the
internet, and every account behind it belongs to somebody in counseling. So the
throttle has two jobs, and the second is easy to lose sight of:

  * **Stop guessing.** Five wrong passwords for one address from one place, and
    that pair is refused for a cooloff period — the correct password included,
    which is the part that makes it a throttle rather than a delay.
  * **Refuse without answering anything.** A locked-out attempt against an
    address with no account must look exactly like one against a counselor's
    address. Otherwise five requests are a way to find out who is being counseled
    here, which is the disclosure this whole application exists to prevent.

And two things it must not do: lock out the church office because one person
mistyped a password on a shared connection, or hand an attacker a way to take a
counselor offline for the evening by guessing at their address five times.

Throttling is switched off in config/settings/test.py — dozens of tests submit a
deliberately wrong password and would otherwise lock each other out — so every
test here turns it back on explicitly.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent, AuditVerb
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.django_db

WRONG_PASSWORD = "not-the-right-password"  # noqa: S105


@pytest.fixture
def throttled(settings):
    settings.AXES_ENABLED = True
    settings.AXES_FAILURE_LIMIT = 5
    settings.AXES_COOLOFF_TIME = timedelta(minutes=15)
    return settings


def attempt(client, email, password=WRONG_PASSWORD, **extra):
    return client.post(
        reverse("accounts:login"),
        {"username": email, "password": password},
        **extra,
    )


def lock_out(client, email, *, limit=5, **extra):
    """Fail enough times to trigger the lockout, and return the last response."""
    response = None
    for _ in range(limit):
        response = attempt(client, email, **extra)
    return response


class TestTheLockout:
    def test_the_right_password_is_refused_while_locked_out(self, client, counselee, throttled):
        """The whole point. A throttle that still lets the correct password
        through is a slow login form, not a control — an attacker who lands on the
        password on attempt six is in."""
        lock_out(client, counselee.email)

        response = attempt(client, counselee.email, password=TEST_PASSWORD)

        assert response.status_code == 429
        assert "_auth_user_id" not in client.session

    def test_attempts_below_the_limit_still_get_the_ordinary_form_back(
        self, client, counselee, throttled
    ):
        """Four typos is a bad evening, not an attack."""
        for _ in range(4):
            response = attempt(client, counselee.email)

        assert response.status_code == 200
        assert attempt(client, counselee.email, password=TEST_PASSWORD).status_code == 302

    def test_a_success_clears_the_counter(self, client, counselee, throttled):
        """Otherwise last week's typos and today's add up to a lockout on a day
        nothing went wrong."""
        for _ in range(4):
            attempt(client, counselee.email)
        assert attempt(client, counselee.email, password=TEST_PASSWORD).status_code == 302
        client.post(reverse("accounts:logout"))

        for _ in range(4):
            response = attempt(client, counselee.email)

        assert response.status_code == 200

    def test_the_lockout_expires_by_itself(self, client, counselee, throttled):
        """A lockout an administrator has to clear is a denial of service handed to
        anybody who knows a counselor's address. The clock is what releases it, so
        the test moves the recorded attempts into the past rather than asserting
        the mechanism exists somewhere."""
        from axes.models import AccessAttempt

        lock_out(client, counselee.email)
        AccessAttempt.objects.update(attempt_time=timezone.now() - timedelta(hours=1))

        assert attempt(client, counselee.email, password=TEST_PASSWORD).status_code == 302


class TestWhoElseIsAffected:
    def test_another_account_from_the_same_place_is_unaffected(
        self, client, counselee, counselor, throttled
    ):
        """A church office is one address behind NAT. Locking the IP would mean one
        person's typos stop everybody else booking or signing in."""
        lock_out(client, counselee.email)

        assert attempt(client, counselor.email, password=TEST_PASSWORD).status_code == 302

    def test_the_same_account_from_elsewhere_is_unaffected(self, client, counselor, throttled):
        """Locking the address alone would let anybody who knows a counselor's
        email lock them out of their own practice from anywhere."""
        lock_out(client, counselor.email)

        response = attempt(
            client,
            counselor.email,
            password=TEST_PASSWORD,
            HTTP_X_FORWARDED_FOR="203.0.113.9",
        )

        assert response.status_code == 302

    def test_the_client_is_the_forwarded_address_not_the_proxy(self, client, counselee, throttled):
        """In production every request arrives from Caddy, so a throttle reading
        REMOTE_ADDR would see one client for the whole internet: the first five
        wrong passwords anywhere would lock out everybody. The IP is resolved by
        the same function the audit trail uses — apps.audit.services.client_ip."""
        lock_out(client, counselee.email, HTTP_X_FORWARDED_FOR="198.51.100.7")

        elsewhere = attempt(
            client,
            counselee.email,
            password=TEST_PASSWORD,
            HTTP_X_FORWARDED_FOR="198.51.100.8",
        )

        assert elsewhere.status_code == 302

    def test_an_emailed_link_still_works_for_a_locked_out_counselee(
        self, client, counselee, throttled, mailoutbox
    ):
        """Deliberate, not an oversight.

        The lockout answers password guessing, and a magic link cannot be guessed
        — it is sent to the counselee's own mailbox, and has its own per-address
        rate limit. Refusing it here would mean an attacker could cut a counselee
        off from their counselor by mistyping their password five times.
        """
        lock_out(client, counselee.email)

        response = client.post(reverse("accounts:magic_link_request"), {"email": counselee.email})

        assert response.status_code == 302
        assert len(mailoutbox) == 1


class TestWhatALockedOutVisitorIsTold:
    def test_it_says_nothing_about_whether_the_account_exists(self, client, counselee, throttled):
        """Two clients, one guessing at a real address and one at an invented one.
        The bodies must be identical, or the difference is the answer."""
        from django.test import Client

        stranger_client = Client()

        real = lock_out(client, counselee.email)
        invented = lock_out(stranger_client, "nobody-here@example.org")

        assert real.status_code == invented.status_code == 429
        assert real.content == invented.content

    def test_it_names_neither_the_address_nor_the_limit(self, client, counselee, throttled):
        """The default django-axes page reports both. Either is a hint worth not
        giving: the address confirms that what was submitted was understood, and
        the limit tells an attacker exactly how to pace the next run.

        The limit is set to four here only so that the digit being searched for is
        not also the number of minutes in the cooloff.
        """
        throttled.AXES_FAILURE_LIMIT = 4

        body = lock_out(client, counselee.email, limit=4).content.decode()

        assert counselee.email not in body
        assert "4" not in body

    def test_it_says_how_long_the_pause_lasts(self, client, counselee, throttled):
        """A page that refuses without saying for how long produces a phone call to
        the counselor, which is the outcome this is meant to avoid."""
        body = lock_out(client, counselee.email).content.decode()

        assert "15 minutes" in body

    def test_it_is_a_429(self, client, counselee, throttled):
        """Not a redirect back to the form: a client that is being refused should be
        told so rather than looping."""
        assert lock_out(client, counselee.email).status_code == 429

    def test_it_carries_the_content_security_policy(self, client, counselee, throttled):
        """Rendered by a callable django-axes invokes from middleware, which is
        exactly the sort of response that quietly misses the headers every other
        page gets."""
        response = lock_out(client, counselee.email)

        assert "default-src 'self'" in response["Content-Security-Policy"]


class TestWhatIsRecorded:
    def test_the_lockout_is_audited_once(self, client, counselee, throttled):
        """Once, when it starts. A client that keeps trying during the cooloff would
        otherwise bury the trail in identical rows — and the trail is the only place
        anybody would notice the attempt at all."""
        lock_out(client, counselee.email)
        for _ in range(3):
            attempt(client, counselee.email)

        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_LOCKED_OUT).get()
        assert event.metadata["email"] == counselee.email
        assert event.metadata["failure_limit"] == 5

    def test_the_failed_attempts_are_audited_too(self, client, counselee, throttled):
        """The lockout row says somebody stopped guessing; these say how long they
        were at it, and are what a run against many addresses looks like."""
        lock_out(client, counselee.email)

        assert AuditEvent.objects.filter(verb=AuditVerb.LOGIN_FAILED).count() == 5

    def test_an_invented_address_is_recorded_as_submitted(self, client, throttled):
        """Recording it is not an assertion that the account exists, and a run of
        lockouts against addresses that do not exist here is the clearest signal
        this system produces that somebody is working through a list."""
        lock_out(client, "nobody-here@example.org")

        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_LOCKED_OUT).get()
        assert event.metadata["email"] == "nobody-here@example.org"
        assert event.actor is None

    def test_the_row_records_where_it_came_from(self, client, counselee, throttled):
        lock_out(client, counselee.email, HTTP_X_FORWARDED_FOR="198.51.100.7")

        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_LOCKED_OUT).get()
        assert event.ip == "198.51.100.7"
        assert event.metadata["ip_address"] == "198.51.100.7"


class TestTheConfiguration:
    def test_it_is_on_unless_something_switches_it_off(self, settings):
        """The test suite disables it, which is exactly the sort of convenience that
        gets copied into a deployment. Nothing but an explicit AXES_ENABLED=false
        should be able to."""
        from config.settings import base

        assert base.AXES_ENABLED is True

    def test_the_lockout_is_by_the_pair_not_by_either_half(self):
        """Stated as a test because the two degenerate configurations each look
        reasonable in isolation, and both are asserted against above."""
        from django.conf import settings

        assert settings.AXES_LOCKOUT_PARAMETERS == [["username", "ip_address"]]
