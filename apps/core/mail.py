"""
Where this deployment's mail configuration comes from, and how it is sealed.

The chain of decisions behind this module, in order, because each one exists
because of a real failure:

  * **The database wins over the environment.** A ministry administrator can fix
    mail from the web UI at the moment they discover it is broken. Editing
    ``.env`` needs shell access to the host and a container restart, which in
    practice means the person who needs mail working is not the person who can
    make it work.
  * **The environment is still read.** An existing install has its values there,
    and ``config/settings/dev.py`` and ``test.py`` swap the backend out entirely.
    So each field falls back rather than requiring a row to exist.
  * **The password is sealed, never stored in the clear.** Same envelope scheme
    as documents: a per-row DEK wrapped under ``BCTRACKER_MASTER_KEY``. A
    database dump therefore contains the mail host and username but not the
    secret, which is the same promise the document store makes.
  * **The provider is not assumed.** Host, port *and* the choice between STARTTLS
    and implicit TLS are all stored, so Google Workspace (587, STARTTLS) and Zoho
    (either 587 STARTTLS or 465 implicit TLS) are a matter of configuration rather
    than of code. What is not configurable is having neither: see the check
    constraint on ``MailSettings``.
  * **Nothing here raises out of an ordinary send.** ``ConfiguredEmailBackend``
    falls back to the settings values if the row cannot be read at all — an
    unmigrated database or a master key that has gone missing must not turn every
    page that sends mail into a 500.
  * **``from_address()`` refuses to return an empty string.** Django's SMTP
    backend raises ``ValueError: Invalid address ""`` from inside ``send_mail``,
    which surfaces as a server error on whatever page triggered it — that is
    exactly how a bootstrapped install with an empty ``DEFAULT_FROM_EMAIL``
    turned "create a counselee" into a 500. A missing address is a configuration
    problem, so it raises ``MailNotConfigured`` and the callers that can degrade
    (see ``apps/accounts/views.py``) offer the invitation link on screen instead.
"""

import logging

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend as SmtpEmailBackend
from django.db.utils import DatabaseError, OperationalError, ProgrammingError

from apps.core.models import MailSettings
from apps.documents import crypto

logger = logging.getLogger(__name__)


class MailNotConfigured(Exception):
    """Raised when a send was attempted with nowhere to send it from."""


def stored_settings() -> MailSettings | None:
    """The configuration row, or None if it cannot be read.

    None covers a database that has not been migrated yet and a database that is
    unreachable. Both are states in which the environment's values are the best
    information available, so the caller falls back rather than failing.
    """
    try:
        return MailSettings.objects.filter(pk=MailSettings.SINGLETON_PK).first()
    except (OperationalError, ProgrammingError, DatabaseError):
        logger.warning("Mail settings could not be read from the database; using the environment.")
        return None


# --- the sealed password --------------------------------------------------


def _dek(row: MailSettings) -> bytes:
    return crypto.unwrap_dek(row.wrapped_dek, row.dek_nonce, storage_key=row.storage_key)


def set_password(row: MailSettings, password: str) -> None:
    """Seal a new mailbox password onto the row, in memory.

    Does not save: the caller is a form that saves the row once, so the sealed
    bytes and the host they belong to land in the same transaction. A fresh DEK
    every time, as in ``store_tokens``, so ciphertext in an old backup cannot be
    opened with anything recoverable from the current row.
    """
    if not password:
        row.wrapped_dek = None
        row.dek_nonce = None
        row.password_sealed = None
        return
    dek = crypto.generate_dek()
    row.wrapped_dek, row.dek_nonce = crypto.wrap_dek(dek, storage_key=row.storage_key)
    row.password_sealed = crypto.encrypt_bytes(
        password.encode(), dek=dek, storage_key=row.storage_key
    )


def password_of(row: MailSettings) -> str:
    """Unseal the stored password, or return "" if there is not a usable one.

    A ``DecryptionError`` here means the master key changed or the row was
    tampered with. Logged loudly and treated as absent: the alternative is every
    email in the application raising, and the recovery is the same either way —
    an administrator re-enters the password.
    """
    if not row.has_password:
        return ""
    try:
        return crypto.decrypt_bytes(
            bytes(row.password_sealed), dek=_dek(row), storage_key=row.storage_key
        ).decode()
    except crypto.DecryptionError:
        logger.error("The stored mail password could not be unsealed. Mail will not authenticate.")
        return ""
    except Exception:  # ImproperlyConfigured from a missing master key, mainly.
        logger.exception("The stored mail password could not be read.")
        return ""


# --- the merged configuration --------------------------------------------


def smtp_config() -> dict:
    """Host, port, encryption, username and password, database over environment."""
    row = stored_settings()
    config = {
        "host": settings.EMAIL_HOST,
        "port": settings.EMAIL_PORT,
        "use_tls": settings.EMAIL_USE_TLS,
        "use_ssl": settings.EMAIL_USE_SSL,
        "username": settings.EMAIL_HOST_USER,
        "password": settings.EMAIL_HOST_PASSWORD,
    }
    if row is None:
        return config
    if row.host:
        # host, port and the encryption pair move together: a row configured for
        # a different provider must not keep the environment's port, and must not
        # keep its STARTTLS when the new host wants implicit TLS on 465 — that
        # combination hangs until the socket times out. Taking them as a set is
        # what stops a half-applied change from being unsendable, or sent in the
        # clear.
        config["host"] = row.host
        config["port"] = row.port
        config["use_tls"] = row.use_tls
        config["use_ssl"] = row.use_ssl
    if row.username:
        config["username"] = row.username
    stored_password = password_of(row)
    if stored_password:
        config["password"] = stored_password
    return config


def from_address() -> str:
    """The From address for every message this application sends.

    Preferring the stored value over ``DEFAULT_FROM_EMAIL`` so an administrator
    can correct it, and preferring the username over nothing because a mailbox
    that authenticates is almost always allowed to send as itself.
    """
    row = stored_settings()
    candidates = [
        getattr(row, "from_email", "") or "",
        settings.DEFAULT_FROM_EMAIL or "",
        getattr(row, "username", "") or "",
        settings.EMAIL_HOST_USER or "",
    ]
    for candidate in candidates:
        if candidate.strip():
            return candidate.strip()
    raise MailNotConfigured(
        "No From address is configured, so this message cannot be sent. Set one "
        "under Email settings."
    )


def unconfigured_reason() -> str:
    """A plain sentence naming what is missing, or "" when nothing is.

    Deliberately says nothing about which backend is in use. It would be tempting
    to call the console backend "configured" because it always succeeds, but this
    answer is also what ``invite_staff`` uses to decide whether to *print* an
    invitation link, and a development install that silently stopped printing the
    link — while the mail went to a console nobody was watching — would be a
    developer locked out of their own database. So the question asked is always
    "are there credentials to send with", and a backend that does not need them
    simply ignores the answer.
    """
    config = smtp_config()
    missing = [
        name
        for name, value in (
            ("a server", config["host"]),
            ("a username", config["username"]),
            ("a password", config["password"]),
        )
        if not value
    ]
    try:
        from_address()
    except MailNotConfigured:
        missing.append("a From address")
    if not missing:
        return ""
    return "Mail is not configured: " + ", ".join(missing) + " is missing."


def mail_is_configured() -> bool:
    """Whether a send has any prospect of working.

    Used to decide whether to *offer* to email something, and to decide whether a
    page must fall back to showing an invitation link on screen. Never to decide
    whether somebody may do something.
    """
    return not unconfigured_reason()


# --- the backend ----------------------------------------------------------


class ConfiguredEmailBackend(SmtpEmailBackend):
    """Django's SMTP backend, with its settings read from the database.

    The argument handling mirrors Django's own: ``send_mail`` passes
    ``username=None, password=None`` explicitly, so ``setdefault`` on kwargs
    would not fill them in. ``None`` means "not overridden", and only then is the
    stored configuration consulted — which keeps ``send_mail(auth_user=...)``
    working for a caller that really does want to send as somebody else.
    """

    def __init__(
        self,
        host=None,
        port=None,
        username=None,
        password=None,
        use_tls=None,
        use_ssl=None,
        **kwargs,
    ):
        config = smtp_config()
        # Only one of the two is ever passed on. Django refuses both at once, and
        # a caller that named one of them means that one: the stored value for the
        # other would otherwise contradict it and raise inside the constructor.
        if use_tls is None and use_ssl is None:
            use_tls, use_ssl = config["use_tls"], config["use_ssl"]
        super().__init__(
            host=host or config["host"] or None,
            port=port or config["port"] or None,
            username=config["username"] if username is None else username,
            password=config["password"] if password is None else password,
            use_tls=use_tls,
            use_ssl=use_ssl,
            **kwargs,
        )


def send_test_message(*, recipient, actor=None) -> None:
    """Send one message to prove the configuration works, and record the result.

    Raises whatever the backend raised, after recording it: the administrator
    pressing the button needs to see the provider's own words. "Username and
    Password not accepted" is actionable; "could not send mail" is not.
    """
    from django.core.mail import send_mail
    from django.utils import timezone

    row = MailSettings.load()
    try:
        send_mail(
            subject="bcTracker mail test",
            message=(
                "This is a test message from bcTracker.\n\n"
                "If you are reading it, the ministry's mail settings work: "
                "invitations, appointment reminders and invoice notices can be "
                "delivered.\n"
            ),
            from_email=from_address(),
            recipient_list=[recipient],
            fail_silently=False,
        )
    except Exception as exc:
        row.last_tested_at = timezone.now()
        # Truncated to the field, and stored rather than only logged so the page
        # can show the last failure to whoever opens it next.
        row.last_test_error = str(exc)[:300] or exc.__class__.__name__
        row.updated_by = actor or row.updated_by
        row.save(update_fields=["last_tested_at", "last_test_error", "updated_by", "updated_at"])
        raise
    row.last_tested_at = timezone.now()
    row.last_test_error = ""
    row.updated_by = actor or row.updated_by
    row.save(update_fields=["last_tested_at", "last_test_error", "updated_by", "updated_at"])
