"""
The session gates in apps/accounts/middleware.py.

The MFA gate is tested through real requests in tests/test_auth_flows.py; what is
tested here is the middleware's own contract — that its exemption list is an
allowlist, that it cannot be quietly disarmed by mis-ordered middleware, and that
the last-seen bookkeeping does not turn every request into a write.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.accounts.middleware import (
    LAST_SEEN_INTERVAL,
    MFA_EXEMPT_URL_NAMES,
    LastSeenMiddleware,
    MFAEnforcementMiddleware,
)

pytestmark = pytest.mark.django_db


def ok(request):
    return HttpResponse("ok")


class TestMFAEnforcementMiddleware:
    def test_the_exemption_list_only_names_routes_that_exist(self):
        """A typo would silently gate a page the enrolment flow depends on."""
        for name in MFA_EXEMPT_URL_NAMES:
            reverse(name)  # raises NoReverseMatch if wrong

    def test_it_refuses_to_run_without_otp_middleware(self, counselor):
        """A wrong MIDDLEWARE order must break loudly, not fail open.

        Without OTPMiddleware there is no is_verified(), and the tempting reading
        of a missing check is "let it through" — which would disable the gate for
        the whole site with no visible symptom.
        """
        request = RequestFactory().get("/")
        request.user = counselor  # no is_verified() attached
        assert not hasattr(counselor, "is_verified")

        with pytest.raises(RuntimeError, match="OTPMiddleware must run before"):
            MFAEnforcementMiddleware(ok)(request)

    def test_an_unlisted_path_is_gated(self, counselor):
        """The allowlist shape means a brand-new route is covered on day one."""
        request = RequestFactory().get("/some/route/added/tomorrow/")
        request.user = counselor
        counselor.is_verified = lambda: False

        response = MFAEnforcementMiddleware(ok)(request)

        assert response.status_code == 302

    def test_static_files_are_not_gated(self, counselor, settings):
        """Otherwise the enrolment page loses its own stylesheet."""
        request = RequestFactory().get(f"{settings.STATIC_URL}css/bctracker.css")
        request.user = counselor
        counselor.is_verified = lambda: False

        assert MFAEnforcementMiddleware(ok)(request).status_code == 200

    def test_a_counselee_is_not_gated(self, counselee):
        request = RequestFactory().get("/")
        request.user = counselee
        counselee.is_verified = lambda: False

        assert MFAEnforcementMiddleware(ok)(request).status_code == 200

    def test_an_account_excepted_from_mfa_is_let_through(self, counselor):
        """mfa_required is stored, not derived, so an exception is possible.

        Whether the ministry ever grants one is a policy question; the point is
        that the flag is what the gate reads, so revoking it has no side channel.
        """
        counselor.mfa_required = False
        request = RequestFactory().get("/")
        request.user = counselor
        counselor.is_verified = lambda: False

        assert MFAEnforcementMiddleware(ok)(request).status_code == 200

    def test_anonymous_requests_pass_to_the_view(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        assert MFAEnforcementMiddleware(ok)(request).status_code == 200


class TestLastSeenMiddleware:
    def test_a_first_request_records_the_time(self, counselor):
        assert counselor.last_seen_at is None
        request = RequestFactory().get("/")
        request.user = counselor

        LastSeenMiddleware(ok)(request)

        counselor.refresh_from_db()
        assert counselor.last_seen_at is not None

    def test_a_second_request_soon_after_does_not_write(self, counselor):
        """One UPDATE per interval, not one per request."""
        counselor.last_seen_at = timezone.now()
        counselor.save(update_fields=["last_seen_at"])
        request = RequestFactory().get("/")
        request.user = counselor

        with mock.patch.object(type(counselor).objects, "filter") as filtered:
            LastSeenMiddleware(ok)(request)

        filtered.assert_not_called()

    def test_a_stale_timestamp_is_refreshed(self, counselor):
        stale = timezone.now() - LAST_SEEN_INTERVAL - timedelta(seconds=1)
        counselor.last_seen_at = stale
        counselor.save(update_fields=["last_seen_at"])
        request = RequestFactory().get("/")
        request.user = counselor

        LastSeenMiddleware(ok)(request)

        counselor.refresh_from_db()
        assert counselor.last_seen_at > stale

    def test_anonymous_requests_are_ignored(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().get("/")
        request.user = AnonymousUser()

        assert LastSeenMiddleware(ok)(request).status_code == 200

    def test_it_works_on_a_real_request(self, client, counselee):
        """Goes through the client, not RequestFactory, on purpose.

        AuthenticationMiddleware installs request.user as a SimpleLazyObject; the
        tests above hand the middleware a plain User and so cannot tell the two
        apart. This one can, and it is the case that actually runs in production.
        """
        client.force_login(counselee)
        assert counselee.last_seen_at is None

        assert client.get(reverse("accounts:home")).status_code == 200

        counselee.refresh_from_db()
        assert counselee.last_seen_at is not None


class TestIdleSessionTimeout:
    """Sessions must not outlive an unattended browser."""

    def test_the_session_expires_on_inactivity(self, settings):
        assert settings.SESSION_COOKIE_AGE <= 60 * 60
        assert settings.SESSION_SAVE_EVERY_REQUEST is True, (
            "without this the cookie age is an absolute lifetime, not an idle "
            "timeout, so an active user would be logged out mid-session"
        )
        assert settings.SESSION_EXPIRE_AT_BROWSER_CLOSE is True
