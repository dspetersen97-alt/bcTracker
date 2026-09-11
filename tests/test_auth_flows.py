"""
Login, second factor, magic links, and invitations.

The assertions here are about the guarantees, not the wiring: staff cannot reach
anything before passing TOTP, a login link is single-use and short-lived, and no
response reveals whether an address has an account.
"""

import re

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import LoginToken, Role, TokenPurpose
from apps.accounts.services import LinkThrottled, invite, request_magic_link
from apps.audit.models import AuditEvent, AuditVerb
from tests.conftest import TEST_PASSWORD

pytestmark = pytest.mark.django_db


def link_from_last_email():
    """Pull the login URL out of the message we just sent."""
    body = mail.outbox[-1].body
    match = re.search(r"https?://\S+/(?:login/link|invitation)/([A-Za-z0-9_-]+)/", body)
    assert match, f"no login link found in:\n{body}"
    return match.group(1)


class TestPasswordLogin:
    def test_counselee_lands_on_the_home_page_with_their_own_page_on_it(self, client, counselee):
        """Login follows through to the home page, which offers this role's pages.

        Two assertions in one on purpose. The first is the whole chain: password
        accepted, no second factor demanded, redirect followed to the landing page.
        The second is what makes landing there acceptable — a counselee who used to
        arrive straight at their case must still be one visible click from it, or
        the home page is a detour rather than a welcome.
        """
        response = client.post(
            "/login/", {"username": counselee.email, "password": TEST_PASSWORD}, follow=True
        )
        assert response.status_code == 200
        assert response.wsgi_request.user == counselee
        assert reverse("core:home") == response.request["PATH_INFO"]
        assert reverse("counseling:my_cases") in response.content.decode()

    def test_login_is_recorded(self, client, counselee):
        client.post("/login/", {"username": counselee.email, "password": TEST_PASSWORD})
        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_SUCCEEDED).get()
        assert event.actor == counselee
        assert event.metadata["method"] == "password"

    def test_a_failed_login_records_the_address_but_not_the_password(self, client, counselee):
        client.post("/login/", {"username": counselee.email, "password": "wrong-password-here"})
        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_FAILED).get()
        assert event.metadata["email"] == counselee.email
        assert "wrong-password-here" not in str(event.metadata)

    def test_the_error_does_not_say_whether_the_address_exists(self, client, counselee):
        """Two failures must be indistinguishable, or the form is an oracle."""
        unknown = client.post("/login/", {"username": "nobody@example.org", "password": "x" * 14})
        known = client.post("/login/", {"username": counselee.email, "password": "x" * 14})
        assert unknown.status_code == known.status_code == 200
        assert unknown.context["form"].errors == known.context["form"].errors

    def test_a_deactivated_account_cannot_log_in(self, client, counselee):
        counselee.is_active = False
        counselee.save(update_fields=["is_active"])

        response = client.post("/login/", {"username": counselee.email, "password": TEST_PASSWORD})

        assert response.status_code == 200
        assert not response.wsgi_request.user.is_authenticated

    def test_a_magic_link_only_account_has_no_usable_password(self, client, make_user):
        """create_user(password=None) must not produce a login with any password."""
        user = make_user(Role.COUNSELEE, password=None, allow_magic_link=True)

        response = client.post("/login/", {"username": user.email, "password": ""})

        assert response.status_code == 200
        assert not response.wsgi_request.user.is_authenticated

    def test_logout_needs_a_post(self, client, counselee):
        client.force_login(counselee)
        assert client.get("/logout/").status_code == 405
        assert client.post("/logout/").status_code == 302


class TestMFAGate:
    """The property that matters most in this app: staff cannot skip TOTP."""

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.COUNSELOR, Role.FINANCIAL_ADMIN])
    def test_staff_are_held_at_enrolment_until_they_verify(self, client, make_user, role):
        staff = make_user(role)
        client.post("/login/", {"username": staff.email, "password": TEST_PASSWORD})

        response = client.get(reverse("accounts:home"))

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:mfa_setup")

    def test_a_counselee_is_never_asked_for_a_second_factor(self, client, counselee):
        client.post("/login/", {"username": counselee.email, "password": TEST_PASSWORD})
        assert client.get(reverse("accounts:home")).status_code == 200

    def test_an_enrolled_staff_user_goes_to_verify_not_setup(self, client, counselor, enrol_totp):
        enrol_totp(counselor)
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})

        response = client.get(reverse("accounts:home"))

        assert response["Location"] == reverse("accounts:mfa_verify")

    def test_a_correct_code_opens_the_session(self, client, counselor, enrol_totp):
        _, code = enrol_totp(counselor)
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})

        client.post("/mfa/verify/", {"code": code()})

        assert client.get(reverse("accounts:home")).status_code == 200
        assert AuditEvent.objects.filter(verb=AuditVerb.MFA_VERIFIED).exists()

    def test_a_wrong_code_is_refused_and_recorded(self, client, counselor, enrol_totp):
        enrol_totp(counselor)
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})

        response = client.post("/mfa/verify/", {"code": "000000"})

        assert response.status_code == 200
        assert client.get(reverse("accounts:home"))["Location"] == reverse("accounts:mfa_verify")
        assert AuditEvent.objects.filter(verb=AuditVerb.MFA_FAILED).exists()

    def test_enrolment_is_refused_once_a_device_exists(self, client, counselor, enrol_totp):
        """Otherwise a stolen half-session could enrol a factor it controls.

        This is the bypass that would make the whole gate pointless, so it is
        blocked in the view as well as by the middleware's choice of redirect.
        """
        enrol_totp(counselor)
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})

        response = client.get(reverse("accounts:mfa_setup"))

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:mfa_verify")

    def test_enrolling_confirms_exactly_one_device(self, client, counselor):
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})
        setup = client.get(reverse("accounts:mfa_setup"))
        assert setup.status_code == 200

        device = TOTPDevice.objects.get(user=counselor, confirmed=False)
        from django_otp.oath import TOTP

        totp = TOTP(device.bin_key, device.step, device.t0, device.digits)
        totp.time = timezone.now().timestamp()

        response = client.post(reverse("accounts:mfa_setup"), {"code": f"{totp.token():06d}"})

        assert response.status_code == 302
        assert TOTPDevice.objects.filter(user=counselor, confirmed=True).count() == 1
        assert client.get(reverse("accounts:home")).status_code == 200
        assert AuditEvent.objects.filter(verb=AuditVerb.MFA_ENROLLED).exists()

    def test_enrolment_reuses_the_pending_secret_across_reloads(self, client, counselor):
        """A reload must not invalidate the QR code the user just scanned."""
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})

        client.get(reverse("accounts:mfa_setup"))
        first = TOTPDevice.objects.get(user=counselor, confirmed=False).key
        client.get(reverse("accounts:mfa_setup"))

        assert TOTPDevice.objects.filter(user=counselor, confirmed=False).count() == 1
        assert TOTPDevice.objects.get(user=counselor, confirmed=False).key == first

    def test_the_gate_applies_to_the_break_glass_admin_too(self, client, superuser):
        """A superuser is staff-role admin, so the admin site is behind TOTP."""
        client.post("/login/", {"username": superuser.email, "password": TEST_PASSWORD})

        response = client.get("/admin/")

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:mfa_setup")

    def test_signing_out_is_always_reachable_mid_enrolment(self, client, counselor):
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})
        assert client.post("/logout/").status_code == 302


class TestMagicLinks:
    def test_an_opted_in_counselee_gets_a_working_link(self, client, counselee):
        client.post("/login/link/", {"email": counselee.email})
        assert len(mail.outbox) == 1
        token = link_from_last_email()

        # GET must not spend the token: mail scanners fetch links.
        assert client.get(f"/login/link/{token}/").status_code == 200
        assert LoginToken.objects.get().consumed_at is None

        response = client.post(f"/login/link/{token}/", follow=True)

        assert response.wsgi_request.user == counselee
        assert LoginToken.objects.get().consumed_at is not None

    def test_a_link_works_only_once(self, client, counselee):
        client.post("/login/link/", {"email": counselee.email})
        token = link_from_last_email()
        client.post(f"/login/link/{token}/")
        client.post("/logout/")

        second = client.post(f"/login/link/{token}/")

        assert second.status_code == 400
        assert not second.wsgi_request.user.is_authenticated

    def test_an_expired_link_is_refused(self, client, counselee, settings):
        settings.MAGIC_LINK_TTL_SECONDS = 1
        client.post("/login/link/", {"email": counselee.email})
        token = link_from_last_email()
        LoginToken.objects.update(expires_at=timezone.now() - timezone.timedelta(seconds=1))

        assert client.post(f"/login/link/{token}/").status_code == 400

    def test_an_unknown_address_looks_exactly_like_a_known_one(self, client, counselee):
        known = client.post("/login/link/", {"email": counselee.email})
        unknown = client.post("/login/link/", {"email": "nobody@example.org"})

        assert known.status_code == unknown.status_code == 302
        assert known["Location"] == unknown["Location"]
        assert len(mail.outbox) == 1  # only the real one was sent

    def test_a_counselee_who_has_not_opted_in_gets_no_link(self, client, make_user):
        user = make_user(Role.COUNSELEE, allow_magic_link=False)

        client.post("/login/link/", {"email": user.email})

        assert mail.outbox == []

    def test_staff_cannot_receive_a_login_link(self, client, counselor):
        """Enforced by a database constraint, so this asserts the flag is off."""
        assert counselor.allow_magic_link is False

        client.post("/login/link/", {"email": counselor.email})

        assert mail.outbox == []

    def test_requests_are_throttled_per_address(self, counselee, settings):
        settings.LOGIN_LINK_MAX_PER_WINDOW = 2

        request_magic_link(email=counselee.email)
        request_magic_link(email=counselee.email)
        with pytest.raises(LinkThrottled):
            request_magic_link(email=counselee.email)

        assert len(mail.outbox) == 2
        assert AuditEvent.objects.filter(verb=AuditVerb.LOGIN_LINK_THROTTLED).exists()

    def test_throttling_still_shows_the_same_page(self, client, counselee, settings):
        settings.LOGIN_LINK_MAX_PER_WINDOW = 1
        client.post("/login/link/", {"email": counselee.email})

        response = client.post("/login/link/", {"email": counselee.email})

        assert response.status_code == 302
        assert response["Location"] == reverse("accounts:magic_link_sent")

    def test_only_a_digest_of_the_token_is_stored(self, counselee):
        request_magic_link(email=counselee.email)
        raw = link_from_last_email()

        stored = LoginToken.objects.get()

        assert raw not in stored.token_hash
        assert stored.token_hash == LoginToken.hash_token(raw)
        assert len(stored.token_hash) == 64

    def test_using_a_link_retires_older_unused_ones(self, client, counselee):
        request_magic_link(email=counselee.email)
        first = link_from_last_email()
        request_magic_link(email=counselee.email)
        second = link_from_last_email()

        client.post(f"/login/link/{second}/")

        assert client.post(f"/login/link/{first}/").status_code == 400

    def test_a_deactivated_account_cannot_use_its_link(self, client, counselee):
        request_magic_link(email=counselee.email)
        token = link_from_last_email()
        counselee.is_active = False
        counselee.save(update_fields=["is_active"])

        response = client.post(f"/login/link/{token}/")

        assert response.status_code == 400
        assert not response.wsgi_request.user.is_authenticated


class TestInvitations:
    def test_an_invitee_sets_a_password_and_lands_signed_in(self, client, make_user, admin_user):
        user = make_user(Role.COUNSELEE, password=None)
        invite(user=user, invited_by=admin_user)
        token = link_from_last_email()

        assert client.get(f"/invitation/{token}/").status_code == 200

        response = client.post(
            f"/invitation/{token}/",
            {"new_password1": "a-good-long-passphrase", "new_password2": "a-good-long-passphrase"},
            follow=True,
        )

        assert response.wsgi_request.user == user
        user.refresh_from_db()
        assert user.check_password("a-good-long-passphrase")

    def test_the_new_session_survives_the_password_it_just_set(self, client, make_user):
        """Django derives the session auth hash from the password.

        Logging in a stale copy of the user produces a hash that no longer matches
        the row, and the invitee is signed out on their next request — which looks
        like the invitation silently failing.
        """
        user = make_user(Role.COUNSELEE, password=None)
        invite(user=user)
        token = link_from_last_email()

        client.post(
            f"/invitation/{token}/",
            {"new_password1": "a-good-long-passphrase", "new_password2": "a-good-long-passphrase"},
        )

        assert client.get(reverse("accounts:home")).status_code == 200

    def test_a_get_does_not_spend_the_invitation(self, client, make_user):
        """A mail scanner following the link must not lock the invitee out."""
        user = make_user(Role.COUNSELEE, password=None)
        invite(user=user)
        token = link_from_last_email()

        client.get(f"/invitation/{token}/")

        assert LoginToken.objects.get().consumed_at is None

    def test_a_weak_password_is_rejected_without_spending_the_token(self, client, make_user):
        user = make_user(Role.COUNSELEE, password=None)
        invite(user=user)
        token = link_from_last_email()

        response = client.post(
            f"/invitation/{token}/", {"new_password1": "short", "new_password2": "short"}
        )

        assert response.status_code == 200
        assert LoginToken.objects.get().consumed_at is None
        user.refresh_from_db()
        assert not user.has_usable_password()

    def test_an_invitation_cannot_be_reused(self, client, make_user):
        user = make_user(Role.COUNSELEE, password=None)
        invite(user=user)
        token = link_from_last_email()
        payload = {
            "new_password1": "a-good-long-passphrase",
            "new_password2": "a-good-long-passphrase",
        }
        client.post(f"/invitation/{token}/", payload)
        client.post("/logout/")

        assert client.post(f"/invitation/{token}/", payload).status_code == 400

    def test_a_magic_link_token_cannot_be_redeemed_as_an_invitation(self, client, counselee):
        """Purposes are separate, so a short-lived link cannot set a password."""
        request_magic_link(email=counselee.email)
        token = link_from_last_email()

        assert client.get(f"/invitation/{token}/").status_code == 400

    def test_sending_an_invitation_is_recorded_against_the_sender(self, make_user, admin_user):
        user = make_user(Role.COUNSELEE, password=None)

        invite(user=user, invited_by=admin_user)

        event = AuditEvent.objects.get(verb=AuditVerb.INVITATION_SENT)
        assert event.actor == admin_user
        assert event.target_id == str(user.pk)


class TestTokenConcurrency:
    def test_a_token_can_only_be_claimed_once(self, counselee):
        """Two simultaneous clicks must not both succeed.

        consume() is a conditional UPDATE for this reason; a check-then-save
        would let both callers through.
        """
        token, _ = LoginToken.issue(
            user=counselee, purpose=TokenPurpose.MAGIC_LINK, ttl_seconds=600
        )
        duplicate = LoginToken.objects.get(pk=token.pk)

        assert token.consume() is True
        assert duplicate.consume() is False
