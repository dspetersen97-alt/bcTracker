"""
The one page in the application that assigns a role.

``manage.py invite_staff`` used to be the only way a counselor or administrator
could come into existence, on the reasoning that role assignment should require
shell access to the host. What that actually produced was a ministry whose
administrator could not add the counselor who started on Monday, and a habit of
handing the server password to whoever was available.

So the page exists, and these tests are the specification of what it must keep
true instead:

  * only ``accounts.manage_users`` — an administrator — may open or post to it,
    and a refusal is recorded;
  * a staff account can never be created with an emailed sign-in link, whatever is
    posted, and the option is not even offered unless the role chosen is a
    counselee's;
  * the profile row that makes the account usable is created with it;
  * nobody's password is set here, and an invitation that cannot be emailed is
    handed over on screen rather than raising.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core import mail as django_mail
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import CounseleeProfile, CounselorProfile

pytestmark = pytest.mark.django_db

URL = reverse("accounts:user_create")
STYLESHEET = Path(settings.BASE_DIR) / "static" / "css" / "bctracker.css"


def payload(**overrides):
    return {
        "role": Role.COUNSELEE,
        "first_name": "Ada",
        "last_name": "Ashford",
        "email": "ada@example.org",
        "phone": "",
        "allow_magic_link": "on",
    } | overrides


class TestCreatingEachKindOfAccount:
    @pytest.mark.parametrize(
        "role,profile_model",
        [
            (Role.COUNSELEE, CounseleeProfile),
            (Role.COUNSELOR, CounselorProfile),
            (Role.ADMIN, None),
            (Role.FINANCIAL_ADMIN, None),
        ],
    )
    def test_the_account_the_profile_and_the_invitation(
        self, client, sign_in, admin_user, role, profile_model
    ):
        sign_in(admin_user)

        response = client.post(URL, payload(role=role))

        assert response.status_code == 302
        user = User.objects.get(email="ada@example.org")
        assert user.role == role
        assert user.has_usable_password() is False, "the password is set from the invitation"
        assert len(django_mail.outbox) == 1
        assert user.email in django_mail.outbox[0].to
        if profile_model is not None:
            assert profile_model.objects.filter(user=user).exists()

    @pytest.mark.parametrize("role", [Role.ADMIN, Role.COUNSELOR, Role.FINANCIAL_ADMIN])
    def test_a_staff_account_never_gets_an_emailed_sign_in_link(
        self, client, sign_in, admin_user, role
    ):
        """Whatever is posted. A mailbox is a weaker factor than a password plus TOTP,
        and a database constraint stands behind this — so the form forces it off
        rather than letting the save fail with an IntegrityError."""
        sign_in(admin_user)

        response = client.post(URL, payload(role=role, allow_magic_link="on"))

        assert response.status_code == 302
        user = User.objects.get(email="ada@example.org")
        assert user.allow_magic_link is False
        assert user.mfa_required is True

    def test_a_counselee_may_have_one(self, client, sign_in, admin_user):
        sign_in(admin_user)

        client.post(URL, payload(role=Role.COUNSELEE, allow_magic_link="on"))

        assert User.objects.get(email="ada@example.org").allow_magic_link is True

    def test_the_creation_is_recorded_against_the_administrator(self, client, sign_in, admin_user):
        sign_in(admin_user)

        client.post(URL, payload(role=Role.COUNSELOR))

        event = AuditEvent.objects.get(verb=AuditVerb.USER_CREATED)
        assert event.actor == admin_user
        assert event.metadata["role"] == Role.COUNSELOR

    def test_an_address_already_in_use_is_refused(self, client, sign_in, admin_user, counselee):
        sign_in(admin_user)

        response = client.post(URL, payload(email=counselee.email.upper()))

        assert response.status_code == 200
        assert "email" in response.context["form"].errors
        assert User.objects.filter(role=Role.COUNSELEE).count() == 1

    def test_the_invitation_link_actually_works(self, client, sign_in, admin_user):
        """End to end: the invitee sets a password and is signed in."""
        import re

        sign_in(admin_user)
        client.post(URL, payload(role=Role.COUNSELEE))
        body = django_mail.outbox[0].body
        path = re.search(r"https?://\S+(/invitation/[A-Za-z0-9_-]+/)", body).group(1)

        client.logout()
        response = client.post(
            path,
            {
                "new_password1": "a-long-enough-passphrase",
                "new_password2": "a-long-enough-passphrase",
            },
        )

        assert response.status_code == 302
        assert User.objects.get(email="ada@example.org").has_usable_password()


class TestTheEmailedLinkOptionIsForCounseleesOnly:
    """The option has to be unavailable for the other three roles, not merely ignored.

    Forcing the value off on the way in is tested above and stays true. What is added
    here is that an administrator creating a counselor is not asked the question in
    the first place, which is done with a stylesheet rule keyed on a checked radio —
    there is no JavaScript in this application to do it any other way. That makes the
    hook the rule keys on part of the contract between three files, so it is tested
    like one: rename the class or the widget and this fails, rather than the checkbox
    quietly coming back for every role.
    """

    def test_the_role_is_asked_with_radio_buttons(self, client, sign_in, admin_user):
        """A ``<select>``'s value is invisible to CSS. A dropdown here would leave
        nothing in the page able to notice the choice changing."""
        sign_in(admin_user)

        page = client.get(URL).content.decode()

        assert 'type="radio"' in page
        for role in Role.values:
            assert f'value="{role}"' in page, role

    def test_the_page_and_the_stylesheet_agree_on_the_hook(self, client, sign_in, admin_user):
        sign_in(admin_user)
        page = client.get(URL).content.decode()

        assert "role-form" in page
        assert 'id="id_allow_magic_link"' in page
        rule = re.search(
            r"\.role-form:not\(:has\((?P<checked>[^)]+)\)\)\s*(?P<target>\S+)\s*\{(?P<body>[^}]*)\}",
            STYLESHEET.read_text(),
        )
        assert rule, "the stylesheet no longer takes the option off the page for staff roles"
        assert rule["checked"] == 'input[name="role"][value="counselee"]:checked'
        assert "#id_allow_magic_link" in rule["target"]
        assert "display: none" in rule["body"]

    def test_a_counselee_created_from_a_case_is_still_offered_it(self, client, sign_in, admin_user):
        """That page has no role question at all — it only ever makes counselees — so
        a rule written without ``.role-form`` in front of it would hide the option on
        the one path where it always applies."""
        sign_in(admin_user)

        page = client.get(reverse("counseling:counselee_create")).content.decode()

        assert 'id="id_allow_magic_link"' in page
        assert "role-form" not in page


class TestWhenMailIsNotWorking:
    def test_the_account_stands_and_the_link_is_shown(self, client, sign_in, admin_user, settings):
        """The state a fresh install is in. It must still be able to take somebody on."""
        settings.EMAIL_HOST_USER = ""
        settings.EMAIL_HOST_PASSWORD = ""
        sign_in(admin_user)

        response = client.post(URL, payload())

        assert response.status_code == 200
        assert not django_mail.outbox
        user = User.objects.get(email="ada@example.org")
        assert CounseleeProfile.objects.filter(user=user).exists()
        link = response.context["invitation_link"]
        assert "/invitation/" in link

    def test_the_shown_link_signs_the_invitee_in(self, client, sign_in, admin_user, settings):
        settings.EMAIL_HOST_USER = ""
        settings.EMAIL_HOST_PASSWORD = ""
        sign_in(admin_user)
        response = client.post(URL, payload())
        path = response.context["invitation_link"].replace(settings.SITE_BASE_URL, "")

        client.logout()
        accepted = client.post(
            path,
            {
                "new_password1": "a-long-enough-passphrase",
                "new_password2": "a-long-enough-passphrase",
            },
        )

        assert accepted.status_code == 302
        assert User.objects.get(email="ada@example.org").has_usable_password()

    def test_a_refused_provider_is_survived_too(self, client, sign_in, admin_user, monkeypatch):
        """A revoked App Password used to be a 500 on the page that creates a person."""

        def explode(**kwargs):
            raise OSError("535 Username and Password not accepted")

        monkeypatch.setattr(services, "_send_invitation_email", explode)
        sign_in(admin_user)

        response = client.post(URL, payload())

        assert response.status_code == 200
        assert "/invitation/" in response.context["invitation_link"]
        event = AuditEvent.objects.get(verb=AuditVerb.INVITATION_SENT)
        assert event.metadata["emailed"] is False
        assert event.metadata["delivery_error"] == "OSError"

    def test_the_form_warns_before_anything_is_created(self, client, sign_in, admin_user, settings):
        settings.EMAIL_HOST_USER = ""
        settings.EMAIL_HOST_PASSWORD = ""
        sign_in(admin_user)

        page = client.get(URL).content.decode()

        assert "Mail is not configured" in page
        assert reverse("core:mail_settings") in page


class TestWhoMayUseIt:
    @pytest.mark.parametrize("role", [Role.COUNSELOR, Role.FINANCIAL_ADMIN, Role.COUNSELEE])
    def test_nobody_but_an_administrator(self, client, sign_in, make_user, role):
        """A counselor who could create an administrator could promote themselves."""
        sign_in(make_user(role))

        assert client.get(URL).status_code == 403
        assert client.post(URL, payload()).status_code == 403
        assert not User.objects.filter(email="ada@example.org").exists()

    def test_the_refusal_is_recorded(self, client, sign_in, counselor):
        sign_in(counselor)

        client.post(URL, payload(role=Role.ADMIN))

        event = AuditEvent.objects.get(verb=AuditVerb.ACCESS_DENIED, actor=counselor)
        assert event.metadata["permission"] == "accounts.manage_users"

    def test_anonymous_is_sent_to_sign_in(self, client):
        response = client.get(URL)

        assert response.status_code == 302
        assert reverse("accounts:login") in response.headers["Location"]


class TestComingBackToWhereTheAdministratorWas:
    def test_next_is_honoured(self, client, sign_in, admin_user):
        """Reached from the New Case page, an administrator wants the case back."""
        sign_in(admin_user)
        destination = reverse("counseling:case_create")

        response = client.post(f"{URL}?next={destination}", payload())

        assert response.headers["Location"] == destination

    def test_an_off_site_next_is_ignored(self, client, sign_in, admin_user):
        sign_in(admin_user)

        response = client.post(f"{URL}?next=https://example.net/", payload())

        assert response.headers["Location"] == URL
