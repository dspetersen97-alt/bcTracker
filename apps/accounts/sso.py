"""
Staff sign-in with a ministry Google account.

What this is for: a counselor or administrator whose ministry identity already
lives in Google Workspace signs in with it, rather than keeping a second password
here. That removes a password from the places passwords get lost, and it means an
account disabled in Workspace can no longer sign in — which is the part that
actually matters when somebody leaves.

Four rules, each of which is the whole point of the corresponding piece of code:

  * **It never creates an account.** An address that matches nobody is refused. A
    sign-in flow that provisions users is a way to get into a system by owning an
    email address, and the four roles here decide who may read counseling notes.
  * **It never grants a role.** The role on the existing row is the only one there
    is; nothing in Google's response is consulted about permissions.
  * **Staff only.** Counselees keep a password or an emailed link. Their addresses
    are personal, which the hosted-domain check would refuse anyway, and there is
    no reason for a counselee's identity to depend on the ministry's Workspace.
  * **It does not replace the second factor.** The plan floated letting Workspace
    2FA satisfy the TOTP requirement. That is declined: Google's response says
    nothing reliable about whether *this* sign-in used a second factor, so
    accepting it would trade a checked guarantee for an assumed one.
    ``MFAEnforcementMiddleware`` runs afterwards exactly as it does after a
    password login.

Deliberately not ``django-allauth``. The plan named it, and it was the right call
before this codebase had its own OAuth code; it is not now. allauth would add its
own user-provisioning flow, a second set of login routes to keep out of the access
matrix, and ``SocialToken`` rows holding tokens in the clear — the thing
apps/scheduling/google/credentials.py exists to avoid. What is left once those are
subtracted is the hundred lines below.

The handshake itself is the same shape as the calendar consent flow, and for the
same reasons — see apps/scheduling/google/oauth.py on why the state parameter and
the ``hd`` claim are load-bearing. Claim reading and the domain rule are shared
with it through ``google_identity``.
"""

import logging
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.urls import reverse

from apps.accounts.google_identity import domain_refusal, id_token_claims
from apps.accounts.models import Role

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret

#: Identity only. No calendar, no Drive, nothing that reads anything. A counselor
#: consenting to sign in is not consenting to hand over their diary; that is a
#: separate, later, opt-in decision made on the Google settings page.
SCOPES = ("openid", "email")

#: Its own key, separate from the calendar flow's. Sharing one would let a
#: calendar consent in one tab validate a sign-in callback in another.
STATE_SESSION_KEY = "google_sso_state"

#: The roles that may sign in this way.
STAFF_ROLES = (Role.ADMIN, Role.COUNSELOR, Role.FINANCIAL_ADMIN)


class SsoRefused(Exception):
    """Why a sign-in did not happen: one message to show, one word to record.

    The split is the point. The message is deliberately identical whether the
    address matched nobody, matched a counselee, or matched a disabled account —
    a sign-in page that distinguishes those is a way to find out who has an
    account here. ``reason`` carries the distinction into the audit trail, where
    the reader is already trusted.
    """

    def __init__(self, message: str, reason: str = "refused"):
        super().__init__(message)
        self.reason = reason


#: The one message shown for every "this account cannot sign in this way" case.
#: The specific reason goes to the audit trail instead.
REFUSED_MESSAGE = (
    "That Google account cannot be used to sign in here. If you are a counselee, "
    "use your password or ask for a sign-in link by email."
)


def is_enabled() -> bool:
    """Whether staff sign-in with Google is available at all.

    Note the domain condition. Unlike the calendar integration, an empty
    ``GOOGLE_WORKSPACE_DOMAIN`` does not degrade to "accept any Google account"
    here — that would be an authentication bypass rather than a laxer calendar
    rule — so the whole feature switches itself off instead. A deploy check
    reports it, so the failure is visible rather than merely safe.
    """
    return bool(
        settings.GOOGLE_SSO_ENABLED
        and settings.GOOGLE_OAUTH_CLIENT_ID
        and settings.GOOGLE_OAUTH_CLIENT_SECRET
        and settings.GOOGLE_WORKSPACE_DOMAIN
    )


def callback_url() -> str:
    """Built from SITE_BASE_URL, never from the request — see the calendar flow."""
    return f"{settings.SITE_BASE_URL}{reverse('accounts:google_login_callback')}"


def start(session) -> str:
    """Mint a state token, remember it, and return where to send the browser."""
    state = secrets.token_urlsafe(32)
    session[STATE_SESSION_KEY] = state

    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        # A hint to the account chooser, not enforcement. The enforcement is the
        # `hd` claim check after the exchange.
        "hd": settings.GOOGLE_WORKSPACE_DOMAIN,
        # No access_type=offline: this flow wants to know who somebody is once, and
        # asking for a refresh token would mean holding long-lived access to an
        # identity we have no further use for.
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def state_is_valid(session, returned: str) -> bool:
    """Compare and consume. One-shot, so a replayed callback fails."""
    expected = session.pop(STATE_SESSION_KEY, None)
    if not expected or not returned:
        return False
    return secrets.compare_digest(expected, returned)


def claims_for(code: str) -> dict:
    """Exchange an authorization code for Google's assertion about the account.

    Returns the id token claims. Raises ``SsoRefused`` for everything else,
    including a network failure — a sign-in either happens or does not, and there
    is nothing to retry in the background.
    """
    payload = {
        "code": code,
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": callback_url(),
        "grant_type": "authorization_code",
    }

    try:
        response = requests.post(
            TOKEN_URL, data=payload, timeout=settings.GOOGLE_HTTP_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        logger.warning("Could not reach Google to exchange a sign-in code: %s", exc)
        raise SsoRefused("Google could not be reached. Please try again.") from exc

    if response.status_code != 200:
        # An authorization code is single-use and short-lived, so this is a stale or
        # replayed callback far more often than it is a real fault.
        logger.warning("Google returned %s exchanging a sign-in code.", response.status_code)
        raise SsoRefused("That sign-in attempt has expired. Please try again.")

    try:
        body = response.json()
    except ValueError as exc:
        raise SsoRefused("Google's response could not be read. Please try again.") from exc

    claims = id_token_claims(body.get("id_token", ""))
    refusal = domain_refusal(claims, required_domain=settings.GOOGLE_WORKSPACE_DOMAIN)
    if refusal:
        raise SsoRefused(refusal)
    return claims


def user_for(claims: dict):
    """The existing staff account for these claims, or a refusal.

    ``iexact`` because Google normalizes addresses and a person who typed theirs
    in with a capital letter should not end up with two identities. Never
    ``get_or_create``: see the module docstring.
    """
    from apps.accounts.models import User

    email = claims.get("email", "")
    user = User.objects.filter(email__iexact=email).first()

    if user is None:
        raise SsoRefused(REFUSED_MESSAGE, "no_such_account")
    if not user.is_active:
        raise SsoRefused(REFUSED_MESSAGE, "inactive")
    if user.role not in STAFF_ROLES:
        raise SsoRefused(REFUSED_MESSAGE, "not_staff")
    return user
