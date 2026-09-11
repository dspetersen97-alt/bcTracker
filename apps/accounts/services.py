"""
Login-link and enrolment services.

Kept out of the views so the rules that matter — throttling, single use, no
account enumeration — are testable without going through HTTP, and so a
management command can invite someone without a request object.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import LoginToken, TokenPurpose, User
from apps.audit.models import AuditVerb
from apps.audit.services import client_ip, record

logger = logging.getLogger(__name__)

#: Name every confirmed TOTP device gets, so "does this user have a second
#: factor" is a single lookup rather than a scan over arbitrary device names.
TOTP_DEVICE_NAME = "primary"


class LinkThrottled(Exception):
    """Raised when an address has already been sent its allowance of links."""


# --- magic links ----------------------------------------------------------


def request_magic_link(*, email, request=None) -> None:
    """Send a login link if the address belongs to an eligible account.

    Returns nothing in every case, deliberately. The caller shows the same
    "check your email" page whether or not the address exists, because a form
    that answers differently is a free membership oracle for a counseling
    ministry — which is a disclosure in itself, before any document is involved.

    Raises LinkThrottled only when the *same* address is over its allowance,
    which is not an enumeration signal: an attacker can trigger it for any
    address, existing or not, since the counter is keyed on the address.
    """
    ip = client_ip(request)
    normalized = User.objects.normalize_email(email).strip()

    _check_throttle(normalized, request=request)

    user = User.objects.filter(email__iexact=normalized, is_active=True).first()
    if user is None or not user.allow_magic_link:
        # Nothing to send. Recorded so a burst of requests for addresses that do
        # not exist is visible, which is what enumeration attempts look like.
        record(
            AuditVerb.MAGIC_LINK_SENT,
            request=request,
            email=normalized,
            sent=False,
            reason="no eligible account",
        )
        return

    token, raw = LoginToken.issue(
        user=user,
        purpose=TokenPurpose.MAGIC_LINK,
        ttl_seconds=settings.MAGIC_LINK_TTL_SECONDS,
        requested_ip=ip,
    )
    _send_link_email(
        user=user,
        raw_token=raw,
        url_name="accounts:magic_link_consume",
        subject="Your bcTracker sign-in link",
        template="accounts/email/magic_link",
        expires_at=token.expires_at,
    )
    record(AuditVerb.MAGIC_LINK_SENT, actor=user, request=request, token_id=token.pk, sent=True)


def invite(*, user, request=None, invited_by=None) -> LoginToken:
    """Send someone the link they use to set their first password."""
    token, _raw = issue_invitation(user=user, request=request, invited_by=invited_by)
    return token


def issue_invitation(*, user, send=True, request=None, invited_by=None) -> tuple[LoginToken, str]:
    """Issue an invitation and return ``(token, raw_token)``.

    ``invite()`` is the form to reach for; this one exists for its ``send=False``
    case, which skips the email and hands the raw link back for delivery by hand.
    That is for exactly one situation: the first administrator of a new
    deployment, who has to exist before there is anybody who could configure
    SMTP. Everywhere else the link should travel by email — a link a person
    carries is a link that gets pasted into a chat window, and this one sets a
    password.

    The raw token exists only in the return value; what is stored is a digest, so
    a caller that loses it has to issue a new invitation.
    """
    token, raw = LoginToken.issue(
        user=user,
        purpose=TokenPurpose.INVITATION,
        ttl_seconds=settings.INVITATION_TTL_SECONDS,
        requested_ip=client_ip(request),
    )
    if send:
        _send_link_email(
            user=user,
            raw_token=raw,
            url_name="accounts:invitation_accept",
            subject="Set up your bcTracker account",
            template="accounts/email/invitation",
            expires_at=token.expires_at,
        )
    record(
        AuditVerb.INVITATION_SENT,
        actor=invited_by,
        target=user,
        request=request,
        token_id=token.pk,
        # Recorded either way: the account can now be claimed by whoever holds the
        # link, and how it got to them is part of that story.
        emailed=send,
    )
    return token, raw


def invitation_path(raw_token: str) -> str:
    """The path an invitation link points at, for a caller that must print one."""
    return reverse("accounts:invitation_accept", kwargs={"token": raw_token})


def consume_token(*, raw_token, purpose, request=None):
    """Return the user a valid token belongs to, or None.

    None covers every failure — unknown, expired, already used, or belonging to
    a deactivated account — because the caller must not tell them apart. Which
    one it was is in the audit trail instead.
    """
    ip = client_ip(request)
    token = LoginToken.objects.filter(
        token_hash=LoginToken.hash_token(raw_token),
        purpose=purpose,
    ).first()

    if token is None:
        record(AuditVerb.LOGIN_FAILED, request=request, method=purpose, reason="unknown token")
        return None
    if not token.consume(ip=ip):
        # Lost the race, expired, or already spent. Same outcome either way.
        record(
            AuditVerb.LOGIN_FAILED,
            actor=token.user,
            request=request,
            method=purpose,
            reason="token not usable",
            token_id=token.pk,
        )
        return None
    if not token.user.is_active:
        record(
            AuditVerb.LOGIN_FAILED,
            actor=token.user,
            request=request,
            method=purpose,
            reason="inactive account",
            token_id=token.pk,
        )
        return None

    # Any other outstanding token of the same purpose is now stale. Retiring
    # them means an older link left in an inbox stops working once a newer one
    # has been used.
    LoginToken.objects.filter(user=token.user, purpose=purpose, consumed_at__isnull=True).exclude(
        pk=token.pk
    ).update(consumed_at=timezone.now(), updated_at=timezone.now())

    return token.user


# --- TOTP -----------------------------------------------------------------


def confirmed_device(user):
    """The user's usable second factor, or None."""
    return TOTPDevice.objects.filter(user=user, confirmed=True).first()


def unconfirmed_device(user, *, create=False):
    """The half-finished enrolment for this user, optionally creating one.

    An unconfirmed device holds a real secret, so enrolment is resumable: a user
    who reloads the page keeps scanning the same QR code instead of silently
    ending up with a device whose secret the app has already thrown away.
    """
    device = TOTPDevice.objects.filter(user=user, confirmed=False).first()
    if device is None and create:
        device = TOTPDevice.objects.create(user=user, name=TOTP_DEVICE_NAME, confirmed=False)
    return device


def confirm_device(*, device, user, request=None):
    """Promote a scanned device to the user's active second factor."""
    device.confirmed = True
    device.save(update_fields=["confirmed"])
    # One device per user: leaving an older one behind would mean a phone that
    # was replaced still works.
    TOTPDevice.objects.filter(user=user, confirmed=True).exclude(pk=device.pk).delete()
    record(AuditVerb.MFA_ENROLLED, actor=user, request=request, device_id=device.pk)
    return device


# --- internals ------------------------------------------------------------


def _send_link_email(*, user, raw_token, url_name, subject, template, expires_at):
    path = reverse(url_name, kwargs={"token": raw_token})
    context = {
        "user": user,
        "url": f"{settings.SITE_BASE_URL}{path}",
        "expires_at": expires_at,
        "site_url": settings.SITE_BASE_URL,
    }
    send_mail(
        subject=subject,
        message=render_to_string(f"{template}.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        # Workspace SMTP gives no bounce reporting, so a send that fails is
        # invisible unless we log it. Not fail_silently: the caller needs to know
        # it could not deliver, or it will tell the user to check an inbox that
        # will never receive anything.
        fail_silently=False,
    )
    logger.info("sent %s email to user %s", template, user.pk)


def _check_throttle(email, *, request=None):
    since = timezone.now() - timedelta(seconds=settings.LOGIN_LINK_WINDOW_SECONDS)
    recent = LoginToken.objects.filter(
        user__email__iexact=email,
        purpose=TokenPurpose.MAGIC_LINK,
        created_at__gte=since,
    ).count()
    if recent >= settings.LOGIN_LINK_MAX_PER_WINDOW:
        record(
            AuditVerb.LOGIN_LINK_THROTTLED,
            request=request,
            email=email,
            window_seconds=settings.LOGIN_LINK_WINDOW_SECONDS,
        )
        raise LinkThrottled(email)
