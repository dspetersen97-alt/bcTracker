"""
Where this deployment's mail configuration comes from, and what it refuses to do.

Three properties are worth more here than the count of assertions:

  * the mailbox password is sealed under the master key, is not readable from the
    row, and is never rendered back to the page that set it;
  * a From address is always resolvable, or sending raises a configuration error
    instead of Django's ``ValueError: Invalid address ""`` — which is how a
    bootstrapped install turned "create a counselee" into a 500;
  * the settings page is reachable by an administrator and by nobody else.
"""

import pytest
from django.core import mail as django_mail
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.core import mail
from apps.core.models import MailSettings

pytestmark = pytest.mark.django_db

SETTINGS_URL = reverse("core:mail_settings")
TEST_URL = reverse("core:mail_test")

#: A complete, valid submission. Kept in one place so a test can change one field.
FORM = {
    "host": "smtp.gmail.com",
    "port": "587",
    "use_tls": "on",
    "username": "counseling@example.org",
    "from_email": "counseling@example.org",
    "password": "an-app-password",  # noqa: S106
}


class TestTheSealedPassword:
    def test_it_round_trips_through_the_master_key(self):
        row = MailSettings.load()

        mail.set_password(row, "hunter-2-is-not-this")
        row.save()

        assert mail.password_of(MailSettings.load()) == "hunter-2-is-not-this"

    def test_the_plaintext_is_nowhere_in_the_row(self):
        row = MailSettings.load()
        mail.set_password(row, "an-app-password")
        row.save()

        stored = MailSettings.objects.values().get(pk=row.pk)

        # Every stored value, as bytes. The password must not be recoverable from
        # any of them — that is the difference between this and .env.
        blob = b"".join(
            bytes(value) if isinstance(value, memoryview | bytes) else str(value).encode()
            for value in stored.values()
        )
        assert b"an-app-password" not in blob

    def test_a_fresh_dek_is_used_each_time(self):
        """Ciphertext in an old backup must not open with anything in the row now."""
        row = MailSettings.load()
        mail.set_password(row, "same-password")
        first = bytes(row.wrapped_dek), bytes(row.password_sealed)
        mail.set_password(row, "same-password")

        assert (bytes(row.wrapped_dek), bytes(row.password_sealed)) != first

    def test_clearing_it_leaves_nothing_behind(self):
        row = MailSettings.load()
        mail.set_password(row, "an-app-password")

        mail.set_password(row, "")

        assert row.password_sealed is None
        assert row.wrapped_dek is None
        assert row.has_password is False

    def test_an_unreadable_password_is_treated_as_absent(self, caplog):
        """A changed master key must not turn every send into an exception."""
        row = MailSettings.load()
        mail.set_password(row, "an-app-password")
        row.password_sealed = bytes(row.password_sealed)[:-1] + b"\x00"

        assert mail.password_of(row) == ""
        assert "could not be unsealed" in caplog.text


class TestTheMergedConfiguration:
    def test_the_database_wins_over_the_environment(self, settings):
        settings.EMAIL_HOST = "smtp.example.net"
        settings.EMAIL_HOST_USER = "from-the-env@example.org"
        row = MailSettings.load()
        row.host = "smtp.gmail.com"
        row.port = 465
        row.use_tls = False
        row.username = "from-the-database@example.org"
        mail.set_password(row, "sealed")
        row.save()

        config = mail.smtp_config()

        assert config["host"] == "smtp.gmail.com"
        assert config["username"] == "from-the-database@example.org"
        assert config["password"] == "sealed"
        # Host, port and TLS move as a set: a row configured for one provider must
        # not keep the environment's port, or a change lands half-applied and the
        # mail goes out in the clear.
        assert (config["port"], config["use_tls"]) == (465, False)

    def test_the_environment_is_used_when_there_is_no_row(self, settings):
        settings.EMAIL_HOST = "smtp.example.net"
        settings.EMAIL_HOST_USER = "from-the-env@example.org"
        settings.EMAIL_HOST_PASSWORD = "from-the-env"  # noqa: S105

        assert MailSettings.objects.count() == 0, "reading must not create the row"
        config = mail.smtp_config()

        assert config["host"] == "smtp.example.net"
        assert config["password"] == "from-the-env"

    def test_the_backend_reads_the_stored_settings(self, settings):
        row = MailSettings.load()
        row.host, row.port, row.use_tls = "smtp.gmail.com", 587, True
        row.username = "counseling@example.org"
        mail.set_password(row, "sealed")
        row.save()

        backend = mail.ConfiguredEmailBackend()

        assert backend.host == "smtp.gmail.com"
        assert backend.username == "counseling@example.org"
        assert backend.password == "sealed"

    def test_an_explicit_argument_still_wins(self):
        """send_mail(auth_user=...) must keep working."""
        row = MailSettings.load()
        row.username = "the-ministry@example.org"
        mail.set_password(row, "sealed")
        row.save()

        backend = mail.ConfiguredEmailBackend(
            username="someone-else@example.org", password="theirs"
        )

        assert backend.username == "someone-else@example.org"
        assert backend.password == "theirs"


class TestTheFromAddress:
    def test_the_stored_address_is_preferred(self, settings):
        settings.DEFAULT_FROM_EMAIL = "from-the-env@example.org"
        row = MailSettings.load()
        row.from_email = "counseling@example.org"
        row.save()

        assert mail.from_address() == "counseling@example.org"

    def test_it_falls_back_through_the_settings_and_the_username(self, settings):
        settings.DEFAULT_FROM_EMAIL = ""
        settings.EMAIL_HOST_USER = ""
        row = MailSettings.load()
        row.username = "the-mailbox@example.org"
        row.save()

        # A mailbox that authenticates is almost always allowed to send as itself.
        assert mail.from_address() == "the-mailbox@example.org"

    def test_with_nothing_configured_it_refuses_rather_than_returning_empty(self, settings):
        """The defect this whole module exists for.

        An empty From address reaches Django as ``ValueError: Invalid address ""``
        from inside send_mail, which surfaces as a 500 on whatever page was
        sending. A configuration error is catchable by the pages that can offer
        the invitation link instead.
        """
        settings.DEFAULT_FROM_EMAIL = ""
        settings.EMAIL_HOST_USER = ""

        with pytest.raises(mail.MailNotConfigured):
            mail.from_address()


class TestWhetherMailIsConfigured:
    def test_a_complete_configuration_reports_nothing_missing(self, settings):
        settings.EMAIL_HOST_PASSWORD = "an-app-password"  # noqa: S105

        assert mail.unconfigured_reason() == ""
        assert mail.mail_is_configured() is True

    def test_each_missing_piece_is_named(self, settings):
        settings.EMAIL_HOST_USER = ""
        settings.EMAIL_HOST_PASSWORD = ""

        reason = mail.unconfigured_reason()

        assert "a username" in reason and "a password" in reason
        assert mail.mail_is_configured() is False

    def test_the_answer_does_not_depend_on_the_backend(self, settings):
        """Deliberately backend-agnostic — see the note in apps/core/mail.py.

        The console backend always succeeds, but this answer also decides whether
        invite_staff prints a link. A development install that stopped printing it
        because "mail works" would be a developer locked out.
        """
        settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
        settings.EMAIL_HOST_USER = ""
        settings.EMAIL_HOST_PASSWORD = ""

        assert mail.mail_is_configured() is False

    def test_the_deploy_check_reports_it_without_blocking_startup(self, settings):
        """Info, not Warning: entrypoint.sh runs --fail-level WARNING.

        A fresh install has no mail and must still boot, or the administrator who
        would configure it cannot sign in to do so.
        """
        from apps.core.checks import check_mail_is_configured

        settings.EMAIL_HOST_USER = ""
        settings.EMAIL_HOST_PASSWORD = ""

        messages = check_mail_is_configured(None)

        assert [message.id for message in messages] == ["mail.I001"]
        assert messages[0].level < 30  # below WARNING


class TestTheSettingsPage:
    def test_an_administrator_saves_a_configuration(self, client, sign_in, admin_user):
        sign_in(admin_user)

        response = client.post(SETTINGS_URL, FORM)

        assert response.status_code == 302
        row = MailSettings.load()
        assert row.host == "smtp.gmail.com"
        assert row.username == "counseling@example.org"
        assert row.updated_by == admin_user
        assert mail.password_of(row) == "an-app-password"

    def test_the_change_is_recorded_without_the_password(self, client, sign_in, admin_user):
        sign_in(admin_user)

        client.post(SETTINGS_URL, FORM)

        event = AuditEvent.objects.get(verb=AuditVerb.MAIL_SETTINGS_UPDATED)
        assert event.actor == admin_user
        assert event.metadata["host"] == "smtp.gmail.com"
        assert "an-app-password" not in str(event.metadata)

    def test_the_password_is_never_rendered_back(self, client, sign_in, admin_user):
        sign_in(admin_user)
        client.post(SETTINGS_URL, FORM)

        page = client.get(SETTINGS_URL).content.decode()

        assert "an-app-password" not in page

    def test_a_username_with_no_password_is_refused(self, client, sign_in, admin_user):
        sign_in(admin_user)

        response = client.post(SETTINGS_URL, FORM | {"password": ""})

        assert response.status_code == 200
        assert response.context["form"].errors

    def test_an_existing_password_survives_an_edit_that_leaves_it_blank(
        self, client, sign_in, admin_user
    ):
        """Blank means "unchanged", or correcting the From line would break sending."""
        sign_in(admin_user)
        client.post(SETTINGS_URL, FORM)

        response = client.post(
            SETTINGS_URL, FORM | {"password": "", "from_email": "office@example.org"}
        )

        assert response.status_code == 302
        row = MailSettings.load()
        assert row.from_email == "office@example.org"
        assert mail.password_of(row) == "an-app-password"

    def test_only_one_row_can_exist(self, client, sign_in, admin_user):
        sign_in(admin_user)
        client.post(SETTINGS_URL, FORM)
        client.post(SETTINGS_URL, FORM | {"host": "smtp.example.net"})

        assert MailSettings.objects.count() == 1

    @pytest.mark.parametrize("role", [Role.COUNSELOR, Role.FINANCIAL_ADMIN, Role.COUNSELEE])
    def test_nobody_else_may_read_or_change_it(self, client, sign_in, make_user, role):
        sign_in(make_user(role))

        assert client.get(SETTINGS_URL).status_code == 403
        assert client.post(SETTINGS_URL, FORM).status_code == 403
        assert MailSettings.objects.count() == 0

    def test_a_refusal_is_recorded(self, client, sign_in, counselor):
        sign_in(counselor)

        client.get(SETTINGS_URL)

        event = AuditEvent.objects.get(verb=AuditVerb.ACCESS_DENIED, actor=counselor)
        assert event.metadata["permission"] == "core.manage_site_settings"


class TestTheTestMessage:
    def test_it_goes_to_the_administrator_and_nowhere_else(self, client, sign_in, admin_user):
        sign_in(admin_user)

        response = client.post(TEST_URL)

        assert response.status_code == 302
        assert django_mail.outbox[-1].to == [admin_user.email]
        assert MailSettings.load().last_tested_at is not None
        assert AuditEvent.objects.filter(verb=AuditVerb.MAIL_TEST_SENT).exists()

    def test_a_failure_is_shown_in_the_provider_s_own_words(
        self, client, sign_in, admin_user, monkeypatch
    ):
        """ "Username and Password not accepted" is actionable; "could not send" is not."""
        sign_in(admin_user)

        def explode(*args, **kwargs):
            raise OSError("535 Username and Password not accepted")

        monkeypatch.setattr("django.core.mail.send_mail", explode)

        response = client.post(TEST_URL, follow=True)

        assert response.status_code == 200
        assert "Username and Password not accepted" in response.content.decode()
        assert MailSettings.load().last_test_error.startswith("535")

    def test_it_is_post_only(self, client, sign_in, admin_user):
        sign_in(admin_user)

        assert client.get(TEST_URL).status_code == 405
