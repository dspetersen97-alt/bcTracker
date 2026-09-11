"""
Storing and refreshing a counselor's Google tokens.

Two responsibilities, kept together because they are the same secret handled at
two moments:

  * **Sealing.** The refresh token never touches a plain column. It is encrypted
    with the document envelope scheme — a fresh per-row DEK wrapped by the master
    key, the row's ``storage_key`` bound in as additional authenticated data — so
    a database dump is not calendar access, and a ciphertext moved between rows
    fails authentication rather than granting the wrong calendar.
  * **Refreshing.** Access tokens last an hour. ``access_token`` hands back a
    valid one, exchanging the refresh token only when the cached one has run out,
    and re-seals the result.

The one asymmetry worth knowing: Google returns a refresh token **only on the
first authorization** for a given client/account pair. A later consent with
``prompt=consent`` returns another, but an ordinary re-authorization may not, so
``store_tokens`` keeps the existing sealed refresh token when handed nothing new.
Overwriting it with an empty value would silently break the connection and the
symptom would appear an hour later, in a cron log.
"""

import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from apps.documents import crypto
from apps.scheduling.google.errors import (
    AuthorizationLost,
    GoogleNotConfigured,
    TransientGoogleError,
)
from apps.scheduling.models import GoogleCredential

logger = logging.getLogger(__name__)

# noqa on the name: S105 reads "TOKEN" and assumes a hardcoded secret. It is a URL.
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

#: Google's own default when it declines to say. An hour is what it has returned
#: for years, but assuming it would mean a token used past its life if that ever
#: changed, and the failure would look like an intermittent 401.
DEFAULT_EXPIRY_SECONDS = 3600


def require_configuration() -> tuple[str, str]:
    """The OAuth client pair, or a refusal naming what is missing."""
    if not settings.GOOGLE_CALENDAR_ENABLED:
        raise GoogleNotConfigured("GOOGLE_CALENDAR_ENABLED is off.")
    client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    client_secret = settings.GOOGLE_OAUTH_CLIENT_SECRET
    if not client_id or not client_secret:
        raise GoogleNotConfigured(
            "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must both be set."
        )
    return client_id, client_secret


# --- sealing --------------------------------------------------------------


def _dek(credential: GoogleCredential) -> bytes:
    return crypto.unwrap_dek(
        credential.wrapped_dek,
        credential.dek_nonce,
        storage_key=credential.storage_key,
    )


def _seal(credential: GoogleCredential, plaintext: str, *, dek: bytes) -> bytes:
    return crypto.encrypt_bytes(
        plaintext.encode(),
        dek=dek,
        storage_key=credential.storage_key,
    )


def _unseal(credential: GoogleCredential, sealed, *, dek: bytes) -> str:
    return crypto.decrypt_bytes(
        bytes(sealed),
        dek=dek,
        storage_key=credential.storage_key,
    ).decode()


def store_tokens(
    *,
    counselor,
    refresh_token: str = "",
    access_token: str = "",
    expires_in: int | None = None,
    google_email: str = "",
    granted_scopes: str = "",
    calendar_id: str = "",
) -> GoogleCredential:
    """Create or update a counselor's connection, sealing whatever was supplied.

    A fresh DEK on every call rather than one reused for the life of the row: a
    reconnection is a new secret, and giving it a new key means the old
    ciphertext in a database backup cannot be unsealed with anything recoverable
    from the current row.

    Called both by the consent callback (which has a refresh token) and by the
    refresh exchange (which usually does not) — hence the empty defaults and the
    "keep what we have" rule for the refresh token.
    """
    credential = GoogleCredential.objects.filter(counselor=counselor).first()
    if credential is None:
        credential = GoogleCredential(counselor=counselor)
        if not refresh_token:
            # Refusing rather than storing a connection that can never refresh.
            # The counselor would see "connected" and nothing would ever sync.
            raise AuthorizationLost(
                "Google did not return a refresh token, so the calendar could not be "
                "connected. Disconnect the app under your Google account's security "
                "settings and try again."
            )

    # Read the existing secrets out before re-keying, so nothing is lost if only
    # one of the two is being replaced.
    previous_refresh = ""
    if credential.pk and bytes(credential.refresh_token_sealed or b""):
        try:
            previous_refresh = _unseal(
                credential, credential.refresh_token_sealed, dek=_dek(credential)
            )
        except crypto.DecryptionError:
            # The master key changed, or the row was tampered with. Either way the
            # stored token is unusable, so this is only recoverable by reconnecting.
            logger.error(
                "Could not unseal the stored Google refresh token for counselor %s.",
                counselor.pk,
            )

    # The storage key is the AAD binding a sealed token to this row, so it is set
    # by the field default at construction and never rotated — rotating it would
    # invalidate the ciphertext being written in the same breath.
    dek = crypto.generate_dek()
    credential.wrapped_dek, credential.dek_nonce = crypto.wrap_dek(
        dek, storage_key=credential.storage_key
    )
    credential.refresh_token_sealed = _seal(credential, refresh_token or previous_refresh, dek=dek)
    if access_token:
        credential.access_token_sealed = _seal(credential, access_token, dek=dek)
        seconds = expires_in or DEFAULT_EXPIRY_SECONDS
        credential.access_token_expires_at = timezone.now() + timedelta(seconds=seconds)
    else:
        # Re-keying invalidated any cached access token, since it was sealed under
        # the previous DEK. Dropped rather than left to fail to unseal later.
        credential.access_token_sealed = None
        credential.access_token_expires_at = None

    if google_email:
        credential.google_email = google_email
    if granted_scopes:
        credential.granted_scopes = granted_scopes
    if calendar_id:
        credential.calendar_id = calendar_id
    # Any successful token exchange means the grant works again.
    credential.revoked_at = None
    credential.last_error = ""
    credential.save()
    return credential


def refresh_token_of(credential: GoogleCredential) -> str:
    """The plaintext refresh token. Kept private to this package."""
    return _unseal(credential, credential.refresh_token_sealed, dek=_dek(credential))


# --- refreshing -----------------------------------------------------------


def access_token(credential: GoogleCredential) -> str:
    """A usable access token, refreshing first if the cached one has run out.

    The leeway matters: checking "has it expired" against the exact moment would
    let a token that expires in two seconds be used for a request that takes
    three, and the resulting 401 looks like an authorization failure rather than a
    clock race.
    """
    if not credential.is_usable:
        raise AuthorizationLost("This Google connection has been revoked.")

    cached = bytes(credential.access_token_sealed or b"")
    if cached and credential.access_token_expires_at:
        leeway = timedelta(seconds=settings.GOOGLE_TOKEN_LEEWAY_SECONDS)
        if credential.access_token_expires_at - leeway > timezone.now():
            try:
                return _unseal(credential, cached, dek=_dek(credential))
            except crypto.DecryptionError:
                # Fall through to a refresh. A cached token that will not unseal is
                # a cache problem, and the refresh token may well be fine.
                logger.warning(
                    "Cached Google access token for counselor %s would not unseal; refreshing.",
                    credential.counselor_id,
                )

    return _refresh(credential)


def _refresh(credential: GoogleCredential) -> str:
    client_id, client_secret = require_configuration()
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token_of(credential),
        "grant_type": "refresh_token",
    }

    try:
        response = requests.post(
            TOKEN_URL, data=payload, timeout=settings.GOOGLE_HTTP_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise TransientGoogleError(f"Could not reach Google to refresh a token: {exc}") from exc

    if response.status_code >= 500:
        raise TransientGoogleError(f"Google returned {response.status_code} refreshing a token.")

    body = _json_or_empty(response)
    if response.status_code != 200:
        error = body.get("error", "")
        if error in ("invalid_grant", "unauthorized_client", "invalid_client"):
            # Terminal. Recorded on the row so the settings page can ask for a
            # reconnection instead of the cron log being the only place it shows.
            mark_revoked(credential, reason=_revocation_message(error))
            raise AuthorizationLost(f"Google refused the refresh token: {error}")
        raise TransientGoogleError(
            f"Google returned {response.status_code} refreshing a token: {error or 'no detail'}"
        )

    token = body.get("access_token", "")
    if not token:
        raise TransientGoogleError("Google returned no access token.")

    store_tokens(
        counselor=credential.counselor,
        access_token=token,
        expires_in=body.get("expires_in"),
        # A refresh response may carry a new refresh token; store_tokens keeps the
        # existing one when this is empty.
        refresh_token=body.get("refresh_token", ""),
    )
    credential.refresh_from_db()
    return token


def _revocation_message(error: str) -> str:
    if error == "invalid_grant":
        return (
            "Google no longer accepts this connection. This happens after a password "
            "change, or if access was withdrawn. Please reconnect your calendar."
        )
    return "Google rejected this connection. Please reconnect your calendar."


def _json_or_empty(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# --- ending a connection --------------------------------------------------


def mark_revoked(credential: GoogleCredential, *, reason: str) -> None:
    """Record that the grant is dead, and drop the secrets.

    The ciphertext goes rather than being kept "in case": a token Google has
    already refused has no remaining use, and keeping a dead credential sealed in
    the database is a liability with no upside.
    """
    credential.revoked_at = timezone.now()
    credential.last_error = reason[:300]
    credential.refresh_token_sealed = b""
    credential.access_token_sealed = None
    credential.access_token_expires_at = None
    credential.save(
        update_fields=[
            "revoked_at",
            "last_error",
            "refresh_token_sealed",
            "access_token_sealed",
            "access_token_expires_at",
            "updated_at",
        ]
    )


def revoke_at_google(credential: GoogleCredential) -> bool:
    """Ask Google to drop the grant. Returns whether it said yes.

    Best effort on purpose. Deleting our copy is what actually ends our access,
    and a counselor clicking "disconnect" must not be blocked by Google being
    slow — so a failure here is logged and the local revocation happens anyway.
    """
    try:
        token = refresh_token_of(credential)
    except crypto.DecryptionError:
        return False
    if not token:
        return False

    try:
        response = requests.post(
            REVOKE_URL,
            data={"token": token},
            timeout=settings.GOOGLE_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.warning("Could not reach Google to revoke a grant: %s", exc)
        return False
    if response.status_code != 200:
        logger.warning("Google returned %s revoking a grant.", response.status_code)
    return response.status_code == 200
