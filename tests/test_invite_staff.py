"""
The only path by which a counselor or administrator comes into existence.

There is no page that does this, so these tests are the whole specification of
staff provisioning: what the command creates, what it refuses, and — the part
that matters on a fresh install — that it can hand over a usable invitation with
no mail configured at all. Without that, the first administrator cannot be
created until SMTP works, and SMTP cannot be configured until somebody can sign
in.
"""

import io
import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import LoginToken, Role, TokenPurpose
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import CounselorProfile

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def run():
    """Run the command, returning stdout."""

    def _run(**options):
        out = io.StringIO()
        call_command("invite_staff", stdout=out, stderr=io.StringIO(), **options)
        return out.getvalue()

    return _run


@pytest.fixture
def no_mail(settings):
    """A deployment where SMTP has not been configured yet."""
    settings.EMAIL_HOST_USER = ""
    settings.EMAIL_HOST_PASSWORD = ""


@pytest.fixture
def with_mail(settings):
    settings.EMAIL_HOST_USER = "counseling@example.org"
    settings.EMAIL_HOST_PASSWORD = "an-app-password"


def link_from(output):
    """The path of the invitation URL the command printed.

    The path rather than the whole URL, so the request goes to the test client's
    own host; what the printed link is prefixed with is SITE_BASE_URL, and that is
    asserted separately.
    """
    match = re.search(r"https?://\S+(/invitation/[A-Za-z0-9_-]+/)", output)
    assert match, f"no invitation link in output:\n{output}"
    return match.group(1)


class TestCreatingTheFirstAdministrator:
    """The bootstrap case: an empty database and no working email."""

    def test_the_account_is_created_and_the_link_is_printed(self, run, no_mail):
        output = run(email="pastor@example.org", role=Role.ADMIN)

        user = User.objects.get(email="pastor@example.org")
        assert user.role == Role.ADMIN
        assert "/invitation/" in output
        assert not mail.outbox

    def test_the_printed_link_actually_works(self, client, run, no_mail):
        """End to end, because a link that is merely well-formed is worthless."""
        output = run(email="pastor@example.org", role=Role.ADMIN)

        response = client.post(
            link_from(output),
            {"new_password1": "a-good-long-passphrase", "new_password2": "a-good-long-passphrase"},
            follow=True,
        )

        user = User.objects.get(email="pastor@example.org")
        assert response.wsgi_request.user == user
        assert user.check_password("a-good-long-passphrase")

    def test_the_new_administrator_still_has_to_enrol_a_second_factor(self, client, run, no_mail):
        """Printing the link is a convenience for the install, not a way around MFA."""
        output = run(email="pastor@example.org", role=Role.ADMIN)
        client.post(
            link_from(output),
            {"new_password1": "a-good-long-passphrase", "new_password2": "a-good-long-passphrase"},
        )
        user = User.objects.get(email="pastor@example.org")

        assert user.mfa_required
        assert not TOTPDevice.objects.filter(user=user, confirmed=True).exists()
        landing = client.get(reverse("accounts:home"))
        assert landing.status_code == 302
        assert landing.url == reverse("accounts:mfa_setup")

    def test_the_account_cannot_be_signed_into_with_a_link_instead(self, run, no_mail):
        """Staff may not use magic links; a database constraint says so too."""
        run(email="pastor@example.org", role=Role.ADMIN)

        assert not User.objects.get(email="pastor@example.org").allow_magic_link


class TestEmailDelivery:
    def test_a_configured_deployment_mails_the_invitation(self, run, with_mail):
        output = run(email="pastor@example.org", role=Role.COUNSELOR)

        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["pastor@example.org"]
        # The link is not also echoed to the terminal: it went to their inbox, and
        # a second copy in a scrollback is a second place it can leak from.
        assert "/invitation/" not in output

    def test_print_link_overrides_a_working_mail_configuration(self, run, with_mail):
        output = run(email="pastor@example.org", role=Role.COUNSELOR, print_link=True)

        assert "/invitation/" in output
        assert not mail.outbox

    def test_a_failure_to_send_leaves_no_account_behind(self, run, with_mail, monkeypatch):
        """The address would otherwise be taken by an account nobody can claim, and
        the next attempt would be refused as a duplicate."""

        def refuse(**kwargs):
            raise OSError("smtp refused")

        monkeypatch.setattr("apps.accounts.services.send_mail", refuse)

        with pytest.raises(OSError, match="smtp refused"):
            run(email="pastor@example.org", role=Role.COUNSELOR)

        assert not User.objects.filter(email="pastor@example.org").exists()
        assert not LoginToken.objects.exists()


class TestWhatItRefuses:
    def test_a_counselee_cannot_be_created_this_way(self, run):
        """They belong to a case, and this command knows nothing about cases."""
        with pytest.raises(CommandError, match="invalid choice"):
            run(email="joel@example.org", role="counselee")

        assert not User.objects.filter(email="joel@example.org").exists()

    def test_an_unknown_role_is_refused_rather_than_stored(self, run):
        with pytest.raises(CommandError, match="invalid choice"):
            run(email="pastor@example.org", role="superuser")

    def test_an_existing_address_is_refused(self, run, make_user, no_mail):
        make_user(Role.COUNSELOR, email="pastor@example.org")

        with pytest.raises(CommandError, match="already has an account"):
            run(email="pastor@example.org", role=Role.COUNSELOR)

    def test_a_second_account_cannot_be_made_by_changing_the_capitalisation(
        self, run, make_user, no_mail
    ):
        make_user(Role.COUNSELOR, email="pastor@example.org")

        with pytest.raises(CommandError, match="already has an account"):
            run(email="Pastor@Example.org", role=Role.COUNSELOR)

        assert User.objects.filter(email__iexact="pastor@example.org").count() == 1

    def test_reinviting_does_not_promote_anybody(self, run, make_user, no_mail):
        """The refusal that matters: --reinvite must not be a way to hand a
        counselor administrative reach over every case in the ministry."""
        make_user(Role.COUNSELOR, email="pastor@example.org")

        with pytest.raises(CommandError, match="does not change roles"):
            run(email="pastor@example.org", role=Role.ADMIN, reinvite=True)

        assert User.objects.get(email="pastor@example.org").role == Role.COUNSELOR

    def test_a_deactivated_account_is_not_reinvited(self, run, make_user, no_mail):
        """Its invitation would be refused at the point of use, so it is refused
        here instead, where the reason can be given."""
        make_user(Role.COUNSELOR, email="pastor@example.org", is_active=False)

        with pytest.raises(CommandError, match="deactivated"):
            run(email="pastor@example.org", role=Role.COUNSELOR, reinvite=True)

    def test_an_unusable_address_is_refused(self, run):
        with pytest.raises(CommandError, match="not a usable email address"):
            run(email="pastor at example dot org", role=Role.ADMIN)


class TestReinviting:
    def test_a_fresh_link_is_issued(self, run, make_user, no_mail):
        user = make_user(Role.COUNSELOR, email="pastor@example.org")

        output = run(email="pastor@example.org", role=Role.COUNSELOR, reinvite=True)

        assert "Re-invited" in output
        assert LoginToken.objects.filter(user=user, purpose=TokenPurpose.INVITATION).count() == 1

    def test_no_second_account_is_created(self, run, make_user, no_mail):
        make_user(Role.COUNSELOR, email="pastor@example.org")

        run(email="pastor@example.org", role=Role.COUNSELOR, reinvite=True)

        assert User.objects.filter(email__iexact="pastor@example.org").count() == 1


class TestWhatEachRoleGets:
    def test_a_counselor_gets_a_practice_to_configure(self, run, no_mail):
        """Their availability page has something to edit before they first sign in."""
        run(email="pastor@example.org", role=Role.COUNSELOR)

        assert CounselorProfile.objects.filter(user__email="pastor@example.org").exists()

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.FINANCIAL_ADMIN])
    def test_no_other_role_gets_one(self, run, no_mail, role):
        run(email="staff@example.org", role=role)

        assert not CounselorProfile.objects.filter(user__email="staff@example.org").exists()

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.COUNSELOR, Role.FINANCIAL_ADMIN])
    def test_every_staff_role_requires_a_second_factor(self, run, no_mail, role):
        run(email="staff@example.org", role=role)

        assert User.objects.get(email="staff@example.org").mfa_required

    def test_the_account_has_no_password_until_the_invitation_is_used(self, run, no_mail):
        run(email="pastor@example.org", role=Role.ADMIN)

        assert not User.objects.get(email="pastor@example.org").has_usable_password()


class TestTheAuditTrail:
    def test_creating_the_account_is_recorded(self, run, no_mail):
        run(email="pastor@example.org", role=Role.ADMIN)
        user = User.objects.get(email="pastor@example.org")

        event = AuditEvent.objects.get(verb=AuditVerb.USER_CREATED)
        assert event.target_id == str(user.pk)
        # Nobody was signed in, and the trail says where it came from rather than
        # leaving an unexplained actorless row.
        assert event.actor is None
        assert event.metadata["source"] == "manage.py invite_staff"
        assert event.metadata["role"] == Role.ADMIN

    def test_the_invitation_records_whether_it_was_emailed(self, run, no_mail):
        """A printed link is a link that travelled through a person. The trail
        should be able to say so afterwards."""
        run(email="pastor@example.org", role=Role.ADMIN)

        event = AuditEvent.objects.get(verb=AuditVerb.INVITATION_SENT)
        assert event.metadata["emailed"] is False

    def test_an_emailed_invitation_says_that_instead(self, run, with_mail):
        run(email="pastor@example.org", role=Role.ADMIN)

        event = AuditEvent.objects.get(verb=AuditVerb.INVITATION_SENT)
        assert event.metadata["emailed"] is True
