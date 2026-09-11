"""
The consent handshake, and the hosted-domain check that makes it safe.

Ordinary three-legged OAuth, with two details that carry weight:

**The state parameter is CSRF protection, not decoration.** Without it, an
attacker can send a counselor a crafted callback URL carrying the attacker's own
authorization code, and the counselor's account ends up connected to the
attacker's calendar — which would then receive every appointment that counselor
books. The state is random, stored in the session, and compared on return.

**The ``hd`` claim is checked against the ministry's domain.** Google will happily
authorize a personal gmail.com account. Counseling appointments must not be
pushed into a calendar the ministry does not administer and cannot revoke, so a
token whose id token carries the wrong ``hd`` is discarded before it is stored.
Both the claim reading and the domain rule live in
``apps.accounts.google_identity``, shared with staff single sign-on so the two
cannot drift apart.

Scopes are the narrowest that do the job: write our own events, read free/busy.
Notably **not** ``calendar`` or ``calendar.readonly`` — nothing here needs to read
the titles of a counselor's other appointments, and asking for that on a consent
screen would be asking a counselor to hand over their family's diary.
"""

import logging
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings

from apps.accounts.google_identity import domain_refusal, id_token_claims
from apps.scheduling.google.credentials import (
    TOKEN_URL,
    require_configuration,
    store_tokens,
)
from apps.scheduling.google.errors import GoogleRefused, TransientGoogleError

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

#: Write events on the counselor's calendars, and read free/busy. `calendar.events`
#: covers the freeBusy query for calendars the grant already reaches.
SCOPES = (
    "https://www.googleapis.com/auth/calendar.events",
    "openid",
    "email",
)

#: Where the state token lives between the redirect out and the callback back.
STATE_SESSION_KEY = "google_oauth_state"


class DomainNotAllowed(GoogleRefused):
    """The authorized account is not on the ministry's Workspace domain."""


def callback_url() -> str:
    """The redirect URI, built from SITE_BASE_URL rather than the request.

    Deliberately not ``request.build_absolute_uri``: Google matches this string
    exactly against the registered redirect URI, and deriving it from the request
    means a proxy header or a Host header can change it — which turns a
    misconfigured proxy into a failed handshake and a spoofable Host into
    something worse.
    """
    from django.urls import reverse

    return f"{settings.SITE_BASE_URL}{reverse('scheduling:google_callback')}"


def start(session) -> str:
    """Mint a state token, remember it, and return the URL to send the browser to."""
    client_id, _secret = require_configuration()
    state = secrets.token_urlsafe(32)
    session[STATE_SESSION_KEY] = state

    params = {
        "client_id": client_id,
        "redirect_uri": callback_url(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # Required for a refresh token at all: without it Google issues an access
        # token only and the connection would stop working within the hour.
        "access_type": "offline",
        # Forces the consent screen even for an account that has approved before,
        # which is what guarantees a refresh token comes back. Without it, a
        # reconnection after a revocation returns no refresh token and the
        # connection is dead on arrival.
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if settings.GOOGLE_WORKSPACE_DOMAIN:
        # A hint only — Google pre-fills the account chooser but does not enforce
        # it. The enforcement is `_check_domain` below, after the exchange.
        params["hd"] = settings.GOOGLE_WORKSPACE_DOMAIN
    return f"{AUTH_URL}?{urlencode(params)}"


def state_is_valid(session, returned: str) -> bool:
    """Compare and consume. One-shot, so a replayed callback fails.

    ``compare_digest`` rather than ``==``: the timing difference is unlikely to be
    exploitable across a network for a value this long, but there is no cost to
    being right about it.
    """
    expected = session.pop(STATE_SESSION_KEY, None)
    if not expected or not returned:
        return False
    return secrets.compare_digest(expected, returned)


def finish(*, counselor, code: str):
    """Exchange the code, check the domain, store the sealed tokens.

    Returns the ``GoogleCredential``. Raises ``DomainNotAllowed`` if the account
    is not on the ministry's domain — before anything is written, so a refused
    account leaves no trace of a connection.
    """
    client_id, client_secret = require_configuration()
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": callback_url(),
        "grant_type": "authorization_code",
    }

    try:
        response = requests.post(
            TOKEN_URL, data=payload, timeout=settings.GOOGLE_HTTP_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise TransientGoogleError(f"Could not reach Google to exchange the code: {exc}") from exc

    if response.status_code >= 500:
        raise TransientGoogleError(f"Google returned {response.status_code} exchanging the code.")

    try:
        body = response.json()
    except ValueError as exc:
        raise TransientGoogleError("Google's token response was not JSON.") from exc

    if response.status_code != 200:
        # An authorization code is single-use and short-lived, so this is usually a
        # stale or replayed callback rather than something to retry.
        raise GoogleRefused(
            f"Google refused the authorization code: {body.get('error', 'no detail')}"
        )

    claims = id_token_claims(body.get("id_token", ""))
    email = claims.get("email", "")
    refusal = domain_refusal(claims)
    if refusal:
        raise DomainNotAllowed(refusal)

    return store_tokens(
        counselor=counselor,
        refresh_token=body.get("refresh_token", ""),
        access_token=body.get("access_token", ""),
        expires_in=body.get("expires_in"),
        google_email=email,
        granted_scopes=body.get("scope", ""),
    )
