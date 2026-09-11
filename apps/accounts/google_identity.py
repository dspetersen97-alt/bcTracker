"""
Reading Google's assertion about *who* an account belongs to.

Two functions, shared by the two places this application talks to Google: staff
single sign-on here in ``accounts``, and the calendar consent flow in
``apps/scheduling/google``. They are together because the rule they encode must
not diverge — a hosted-domain check that is stricter for signing in than for
connecting a calendar would be a gap somebody eventually walks through.

They are plain functions returning plain values rather than raising, so each
caller can raise the exception its own layer already has a taxonomy for.
"""

import base64
import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def id_token_claims(id_token: str) -> dict:
    """The payload of a JWT received directly from Google's token endpoint.

    Not signature-verified, and that is a deliberate, bounded exception: the
    string came back over TLS from ``oauth2.googleapis.com`` in response to a POST
    carrying our client secret. It did not pass through the browser, so there is
    no party between Google and us who could have substituted it.

    The same shortcut would be a vulnerability for a token arriving any other way
    — from a redirect, a form post, or a client-side flow. If a caller is ever
    added that reads a token from one of those, it must verify the signature
    against Google's JWKS instead of calling this.
    """
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        logger.warning("Could not read the claims from Google's id token.")
        return {}


def domain_refusal(claims: dict, *, required_domain: str = "") -> str:
    """Why this account is not acceptable, or "" if it is.

    The check is against the ``hd`` claim, **not** the email suffix. A Workspace
    domain can have aliases, and ``hd`` is the assertion Google actually makes
    about which organization owns the account; matching on the address would
    accept a lookalike domain that merely ends the right way.

    An empty ``required_domain`` means no restriction, which is allowed only
    because a development machine has no Workspace. Callers that cannot tolerate
    it — staff sign-in, where "any Google account" would be an authentication
    bypass — must refuse to run at all rather than passing an empty domain here.
    """
    domain = required_domain or settings.GOOGLE_WORKSPACE_DOMAIN
    if not domain:
        logger.warning("GOOGLE_WORKSPACE_DOMAIN is unset; accepting any Google account.")
        return ""

    if claims.get("hd") != domain:
        return (
            f"That account is not on {domain}. Please use your ministry "
            "Google account rather than a personal one."
        )
    # Absent means "not asserted either way", and Google always sends it for a
    # Workspace account, so the default is the permissive one only for the case
    # where the claim is missing entirely on an already domain-verified account.
    if not claims.get("email_verified", True):
        return "Google has not verified that address."
    if not claims.get("email"):
        return "Google did not say which address this is."
    return ""
