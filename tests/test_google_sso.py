"""
Staff sign-in with a ministry Google account.

apps/accounts/sso.py states four rules. This file exists to make each of them
cost something to break:

  * **It never creates an account.** An address Google vouches for that matches
    no row here is refused, and the user table is the same size afterwards.
  * **It never grants a role.** Nothing in Google's response is consulted about
    permissions; a counselee with a ministry address is still a counselee, and is
    refused.
  * **Staff only**, and the refusal message is the *same* one for "no such
    account", "not staff", and "disabled" — otherwise the sign-in page is a way to
    find out who has an account here.
  * **It does not replace the second factor.** A successful Google sign-in lands
    on the TOTP page, not on a dashboard. This is the one that would be easiest to
    "improve" away, so it is asserted end to end through the real middleware.

Nothing here touches the network: ``sso`` calls ``requests`` from exactly one
place, so a fake over ``sso.requests.post`` is a complete seam.
"""

import base64
import json

import pytest
import requests
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.accounts import sso
from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.scheduling.google import oauth

pytestmark = pytest.mark.django_db

User = get_user_model()

DOMAIN = "ministry.example.org"


# --- fakes ----------------------------------------------------------------


class FakeResponse:
    """Enough of a ``requests.Response`` for the two things ``sso`` reads.

    ``payload=None`` means "not JSON" — an HTML error page from something in front
    of the API, which the flow has to refuse rather than raise ``ValueError`` out
    of. Kept local rather than shared with tests/test_google_calendar.py: the two
    modules assert against different code, and a shared fake grows options until
    it is a second implementation.
    """

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def id_token_for(claims: dict) -> str:
    """A JWT-shaped string carrying these claims.

    Unsigned, because ``google_identity.id_token_claims`` deliberately does not
    verify a token that came straight from the token endpoint over TLS. Signing it
    here would assert something the code does not do.
    """
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def token_response(**claims):
    """Google's answer to a code exchange, carrying the given claims."""
    ministry_claims = {"hd": DOMAIN, "email_verified": True} | claims
    return FakeResponse(200, {"id_token": id_token_for(ministry_claims)})


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def sso_on(settings):
    """The feature configured as a ministry with a Workspace domain has it."""
    settings.GOOGLE_SSO_ENABLED = True
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id.apps.googleusercontent.com"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.GOOGLE_WORKSPACE_DOMAIN = DOMAIN
    settings.SITE_BASE_URL = "https://counseling.example.org"
    return settings


@pytest.fixture
def staff_counselor(make_user):
    """A counselor whose address is on the ministry's Workspace domain."""
    return make_user(Role.COUNSELOR, email=f"grace@{DOMAIN}")


@pytest.fixture
def start_handshake(client, sso_on):
    """Do the first half for real, and return the state Google would send back.

    Building the session by hand would let a change to ``start`` pass every test
    below while leaving the live flow broken.
    """

    def _start():
        response = client.post(reverse("accounts:google_login"))
        assert response.status_code == 302
        return client.session[sso.STATE_SESSION_KEY]

    return _start


# --- whether the feature exists at all ------------------------------------


class TestWhenItIsAvailable:
    def test_it_is_off_by_default(self):
        """A ministry that has not asked for this does not have a Google button."""
        assert not sso.is_enabled()

    def test_it_is_on_when_fully_configured(self, sso_on):
        assert sso.is_enabled()

    def test_it_switches_itself_off_without_a_workspace_domain(self, sso_on):
        """The important one.

        With no domain to check, "sign in with Google" would mean any Google
        account anywhere whose address happens to match a staff address — an
        authentication bypass. So the feature disables itself rather than
        degrading, and accounts.E002 makes that visible.
        """
        sso_on.GOOGLE_WORKSPACE_DOMAIN = ""

        assert not sso.is_enabled()

    @pytest.mark.parametrize("blank", ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"])
    def test_it_is_off_without_an_oauth_client(self, sso_on, blank):
        setattr(sso_on, blank, "")

        assert not sso.is_enabled()

    def test_the_login_page_offers_it_only_when_it_would_work(self, client, sso_on):
        """A button that reports "not available here" is worse than no button."""
        with_google = client.get(reverse("accounts:login")).content.decode()

        sso_on.GOOGLE_SSO_ENABLED = False
        without = client.get(reverse("accounts:login")).content.decode()

        assert reverse("accounts:google_login") in with_google
        assert reverse("accounts:google_login") not in without

    def test_the_login_page_still_offers_the_password_and_the_link(self, client, sso_on):
        """Google is an addition for staff, not a replacement for the counselees."""
        body = client.get(reverse("accounts:login")).content.decode()

        assert 'name="password"' in body
        assert reverse("accounts:magic_link_request") in body


# --- the state parameter --------------------------------------------------


class TestTheStateParameter:
    """A plain dict stands in for the session here, because that is all
    ``state_is_valid`` uses. The test client's ``session`` property reloads from the
    database on every access, so a consumed state would silently reappear and the
    one-shot assertion below would pass without meaning anything. That the *live*
    flow consumes it is asserted through the views, in
    ``TestSigningIn.test_a_callback_cannot_be_replayed``."""

    def test_it_is_one_shot(self):
        session = {sso.STATE_SESSION_KEY: "a-state"}

        assert sso.state_is_valid(session, "a-state") is True
        # A callback replayed from history or a proxy log must not sign anybody in.
        assert sso.state_is_valid(session, "a-state") is False

    @pytest.mark.parametrize("returned", ["", "some-other-state"])
    def test_anything_else_is_refused(self, returned):
        session = {sso.STATE_SESSION_KEY: "a-state"}

        assert sso.state_is_valid(session, returned) is False

    def test_a_callback_with_no_handshake_behind_it_is_refused(self):
        assert sso.state_is_valid({}, "a-state") is False

    def test_it_does_not_share_a_key_with_the_calendar_flow(self):
        """Sharing one would let a calendar consent in one tab validate a sign-in
        callback in another — two different decisions, made by two different
        clicks, one of which is authentication."""
        assert sso.STATE_SESSION_KEY != oauth.STATE_SESSION_KEY


class TestWhereTheBrowserIsSent:
    def test_it_asks_for_identity_and_nothing_else(self, client, sso_on):
        url = sso.start(client.session)

        assert url.startswith(sso.AUTH_URL)
        assert "scope=openid+email" in url
        # Signing in is not consent to hand over a diary. That is a separate,
        # later, opt-in decision made on the Google settings page.
        assert "calendar" not in url

    def test_it_does_not_ask_for_offline_access(self, client, sso_on):
        """A refresh token would be long-lived access to an identity we want once."""
        assert "access_type=offline" not in sso.start(client.session)

    def test_it_hints_the_ministry_domain_to_the_account_chooser(self, client, sso_on):
        """A hint, not enforcement — the enforcement is the ``hd`` claim check."""
        assert f"hd={DOMAIN}" in sso.start(client.session).replace("%40", "@")

    def test_starting_needs_a_post(self, client, sso_on):
        """A GET would let a link in an email start a handshake and mint a state."""
        assert client.get(reverse("accounts:google_login")).status_code == 405


# --- whose Google account -------------------------------------------------


class TestTheHostedDomainCheck:
    def _claims(self, monkeypatch, **claims):
        monkeypatch.setattr(sso.requests, "post", lambda *a, **kw: token_response(**claims))
        return sso.claims_for("an-authorization-code")

    def test_a_ministry_account_is_accepted(self, sso_on, monkeypatch):
        claims = self._claims(monkeypatch, email=f"grace@{DOMAIN}")

        assert claims["email"] == f"grace@{DOMAIN}"

    def test_a_personal_account_is_refused(self, sso_on, monkeypatch):
        with pytest.raises(sso.SsoRefused) as refusal:
            self._claims(monkeypatch, email="grace@gmail.com", hd=None)

        assert DOMAIN in str(refusal.value)

    def test_a_lookalike_domain_is_refused(self, sso_on, monkeypatch):
        """The check is on the ``hd`` claim, not on the address.

        This account's address ends exactly the right way. Google's assertion
        about who *owns* it says otherwise, and that is the assertion that counts —
        anybody can register a domain whose name ends in another one.
        """
        with pytest.raises(sso.SsoRefused):
            self._claims(
                monkeypatch,
                email=f"grace@evil.{DOMAIN}",
                hd=f"evil.{DOMAIN}",
            )

    def test_an_unverified_address_is_refused(self, sso_on, monkeypatch):
        with pytest.raises(sso.SsoRefused):
            self._claims(monkeypatch, email=f"grace@{DOMAIN}", email_verified=False)

    def test_a_response_naming_no_address_is_refused(self, sso_on, monkeypatch):
        with pytest.raises(sso.SsoRefused):
            self._claims(monkeypatch)


class TestTheCodeExchange:
    def test_a_stale_code_is_refused(self, sso_on, monkeypatch):
        """An authorization code is single-use and short-lived, so a non-200 here is
        a replayed or back-buttoned callback far more often than a real fault."""
        monkeypatch.setattr(sso.requests, "post", lambda *a, **kw: FakeResponse(400, {}))

        with pytest.raises(sso.SsoRefused, match="expired"):
            sso.claims_for("a-stale-code")

    def test_google_being_unreachable_is_refused_not_retried(self, sso_on, monkeypatch):
        """A sign-in either happens or does not; there is nothing to retry later."""

        def unreachable(*args, **kwargs):
            raise requests.ConnectionError("no route to host")

        monkeypatch.setattr(sso.requests, "post", unreachable)

        with pytest.raises(sso.SsoRefused, match="could not be reached"):
            sso.claims_for("a-code")

    def test_a_response_that_is_not_json_is_refused(self, sso_on, monkeypatch):
        monkeypatch.setattr(sso.requests, "post", lambda *a, **kw: FakeResponse(200))

        with pytest.raises(sso.SsoRefused):
            sso.claims_for("a-code")

    def test_the_redirect_uri_is_built_from_the_configured_address(self, sso_on):
        """Never from the request: a Host header is attacker-controlled, and this
        string has to match Google's registration character for character."""
        assert sso.callback_url() == (
            f"https://counseling.example.org{reverse('accounts:google_login_callback')}"
        )


# --- which accounts may sign in -------------------------------------------


class TestWhichAccountsMaySignIn:
    def _claims(self, email):
        return {"email": email, "hd": DOMAIN, "email_verified": True}

    @pytest.mark.parametrize("role_fixture", ["admin_user", "counselor", "financial_admin"])
    def test_every_staff_role_may(self, request, role_fixture):
        user = request.getfixturevalue(role_fixture)

        assert sso.user_for(self._claims(user.email)) == user

    def test_a_counselee_may_not(self, counselee):
        """Not a judgement about the person: their address is personal, they have a
        password and an emailed link, and there is no reason for a counselee's
        identity to depend on the ministry's Workspace."""
        with pytest.raises(sso.SsoRefused) as refusal:
            sso.user_for(self._claims(counselee.email))

        assert refusal.value.reason == "not_staff"

    def test_an_unknown_address_may_not_and_no_account_appears(self, db):
        """The rule that keeps owning an email address from being a way in."""
        before = User.objects.count()

        with pytest.raises(sso.SsoRefused) as refusal:
            sso.user_for(self._claims(f"stranger@{DOMAIN}"))

        assert refusal.value.reason == "no_such_account"
        assert User.objects.count() == before

    def test_a_disabled_account_may_not(self, counselor):
        """Which is most of the point of this flow: somebody who has left."""
        counselor.is_active = False
        counselor.save(update_fields=["is_active"])

        with pytest.raises(sso.SsoRefused) as refusal:
            sso.user_for(self._claims(counselor.email))

        assert refusal.value.reason == "inactive"

    def test_the_three_refusals_read_identically(self, counselee, counselor):
        """Distinguishing them would make this page an account-existence oracle.

        The distinction is not lost — it goes to the audit trail, where the reader
        is already trusted.
        """
        counselor.is_active = False
        counselor.save(update_fields=["is_active"])

        messages = set()
        for email in [counselee.email, counselor.email, f"stranger@{DOMAIN}"]:
            with pytest.raises(sso.SsoRefused) as refusal:
                sso.user_for(self._claims(email))
            messages.add(str(refusal.value))

        assert messages == {sso.REFUSED_MESSAGE}

    def test_the_address_is_matched_without_regard_to_case(self, counselor):
        """Google normalizes addresses; somebody who typed theirs in with a capital
        letter should not turn into a second identity, or a refusal."""
        assert sso.user_for(self._claims(counselor.email.upper())) == counselor

    def test_no_role_comes_from_google(self, counselee):
        """Claims saying so do not make a counselee staff. There is no code that
        reads them, and this is the test that notices if some appears."""
        claims = self._claims(counselee.email) | {
            "role": "admin",
            "groups": ["administrators"],
            "is_staff": True,
        }

        with pytest.raises(sso.SsoRefused):
            sso.user_for(claims)

        counselee.refresh_from_db()
        assert counselee.role == Role.COUNSELEE


# --- the whole flow through the views -------------------------------------


class TestSigningIn:
    def _callback(self, client, state, **params):
        return client.get(
            reverse("accounts:google_login_callback"),
            {"state": state, "code": "an-authorization-code"} | params,
        )

    def test_a_staff_member_signs_in_and_still_owes_a_code(
        self, client, sso_on, staff_counselor, enrol_totp, start_handshake, monkeypatch
    ):
        """The fail-closed promise, asserted through the real middleware.

        Workspace may well have demanded a second factor of this person moments
        ago. Google's response does not say so in any way we can rely on, and
        trading a checked guarantee for an assumed one is not an improvement.
        """
        enrol_totp(staff_counselor)
        state = start_handshake()
        monkeypatch.setattr(
            sso.requests, "post", lambda *a, **kw: token_response(email=staff_counselor.email)
        )

        response = self._callback(client, state)
        landing = client.get(reverse("counseling:dashboard"))

        assert response.status_code == 302
        assert landing["Location"] == reverse("accounts:mfa_verify")

    def test_the_sign_in_is_audited_as_a_google_one(
        self, client, sso_on, staff_counselor, start_handshake, monkeypatch
    ):
        state = start_handshake()
        monkeypatch.setattr(
            sso.requests, "post", lambda *a, **kw: token_response(email=staff_counselor.email)
        )

        self._callback(client, state)

        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_SUCCEEDED).get()
        assert event.actor == staff_counselor
        assert event.metadata["method"] == "google"

    def test_a_callback_cannot_be_replayed(
        self, client, sso_on, staff_counselor, start_handshake, monkeypatch
    ):
        """The live half of the one-shot state.

        A callback URL carries a code and a state in the query string, so it lands
        in browser history, in a proxy log, and in anything that shoulder-surfs a
        URL. Replaying it must get nowhere even though it was genuine once.
        """
        state = start_handshake()
        monkeypatch.setattr(
            sso.requests, "post", lambda *a, **kw: token_response(email=staff_counselor.email)
        )
        self._callback(client, state)
        client.post(reverse("accounts:logout"))

        replayed = self._callback(client, state)

        assert replayed["Location"] == reverse("accounts:login")
        assert "_auth_user_id" not in client.session
        assert AuditEvent.objects.filter(
            verb=AuditVerb.LOGIN_FAILED, metadata__reason="state_mismatch"
        ).exists()

    def test_a_forged_callback_is_refused_and_recorded(self, client, sso_on):
        """Checked before anything else. Doing the exchange first would mean acting
        on a callback before noticing it was forged."""
        response = self._callback(client, "a-state-nobody-issued")

        assert response.status_code == 302
        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_FAILED).get()
        assert event.metadata["reason"] == "state_mismatch"
        assert event.metadata["method"] == "google"

    def test_a_refusal_is_recorded_with_the_address_and_the_reason(
        self, client, sso_on, counselee, start_handshake, monkeypatch
    ):
        """A run of these against one address is the signal worth having."""
        state = start_handshake()
        monkeypatch.setattr(
            sso.requests, "post", lambda *a, **kw: token_response(email=counselee.email)
        )

        response = self._callback(client, state)

        assert response.status_code == 302
        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_FAILED).get()
        assert event.metadata["reason"] == "not_staff"
        assert event.metadata["email"] == counselee.email

    def test_a_refused_account_is_not_signed_in(
        self, client, sso_on, counselee, start_handshake, monkeypatch
    ):
        state = start_handshake()
        monkeypatch.setattr(
            sso.requests, "post", lambda *a, **kw: token_response(email=counselee.email)
        )

        self._callback(client, state)

        assert "_auth_user_id" not in client.session

    def test_dismissing_the_account_chooser_is_not_an_error(self, client, sso_on, start_handshake):
        """Somebody deciding not to sign in with Google after all does not deserve a
        red banner, and is not worth an audit row either."""
        state = start_handshake()

        response = self._callback(client, state, code="", error="access_denied")

        assert response["Location"] == reverse("accounts:login")
        assert not AuditEvent.objects.filter(verb=AuditVerb.LOGIN_FAILED).exists()

    def test_nothing_happens_when_the_feature_is_off(self, client, staff_counselor):
        """Both halves refuse, not only the one behind the button — the routes exist
        whether or not the button is rendered."""
        started = client.post(reverse("accounts:google_login"))
        called_back = client.get(reverse("accounts:google_login_callback"))

        assert started["Location"] == reverse("accounts:login")
        assert called_back["Location"] == reverse("accounts:login")
        assert "_auth_user_id" not in client.session

    def test_an_exchange_failure_records_no_address(
        self, client, sso_on, start_handshake, monkeypatch
    ):
        """There is nothing Google told us to record, so the row says so rather than
        naming whoever the browser happened to belong to."""
        state = start_handshake()
        monkeypatch.setattr(sso.requests, "post", lambda *a, **kw: FakeResponse(400, {}))

        self._callback(client, state)

        event = AuditEvent.objects.filter(verb=AuditVerb.LOGIN_FAILED).get()
        assert event.metadata["email"] == ""


# --- deploy checks --------------------------------------------------------


class TestTheDeployChecks:
    """``is_enabled()`` already refuses to run a half-configured flow. These make a
    silently disabled one *visible*, which is the difference between a ministry
    finding out at deploy time and finding out when nobody can sign in."""

    def _run(self):
        from django.core.checks import run_checks

        return {problem.id for problem in run_checks(include_deployment_checks=True)}

    def test_a_feature_left_off_raises_nothing(self, settings):
        settings.GOOGLE_SSO_ENABLED = False

        assert not {"accounts.E001", "accounts.E002"} & self._run()

    def test_switching_it_on_without_a_client_is_reported(self, sso_on):
        sso_on.GOOGLE_OAUTH_CLIENT_ID = ""

        assert "accounts.E001" in self._run()

    def test_switching_it_on_without_a_domain_is_reported(self, sso_on):
        sso_on.GOOGLE_WORKSPACE_DOMAIN = ""

        assert "accounts.E002" in self._run()

    def test_a_fully_configured_feature_passes(self, sso_on):
        assert not {"accounts.E001", "accounts.E002"} & self._run()
