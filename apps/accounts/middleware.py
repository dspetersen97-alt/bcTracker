"""
Session gates.

``MFAEnforcementMiddleware`` is the reason "staff must use TOTP" is a property of
the system rather than a promise about how views were written. Putting it in
middleware means a new view is covered the moment it is added, and forgetting a
decorator cannot open a hole. It is written as an allowlist: anything not
explicitly named below is gated.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

#: URL names a staff user may reach before passing TOTP. Keep this as short as it
#: can possibly be — every entry is a view that runs for a half-authenticated
#: session. All of these either end the session or advance the enrolment.
MFA_EXEMPT_URL_NAMES = (
    "accounts:mfa_setup",
    "accounts:mfa_verify",
    "accounts:logout",
    "accounts:login",
    "healthz",
)

#: How stale User.last_seen_at is allowed to get. Bounds the write rate to one
#: UPDATE per user per interval rather than one per request.
LAST_SEEN_INTERVAL = timedelta(minutes=5)


def _verification_check(user):
    """``is_verified``, or a loud failure if the middleware order is wrong.

    ``is_verified()`` is added to the user by django_otp's OTPMiddleware. If it is
    missing, settings are misconfigured, and the safe reading of an absent
    verification is "not verified" — never "allow". Raising says so where a
    ``getattr(..., False)`` would silently gate nobody.
    """
    check = getattr(user, "is_verified", None)
    if check is None:
        raise RuntimeError(
            "OTPMiddleware must run before MFAEnforcementMiddleware; "
            "check MIDDLEWARE in config/settings/base.py."
        )
    return check


def session_is_fully_authenticated(request) -> bool:
    """Whether this session has cleared every gate, and not just the password.

    The single answer to "is this person signed in", shared by the middleware below
    and by the navigation the templates render — which is the point of it being a
    function. A staff session between the password and the TOTP code is
    authenticated as far as ``django.contrib.auth`` is concerned: ``request.user``
    is set and ``is_authenticated`` is True, and it can still reach nothing but the
    enrolment pages. Showing it the menu offers a way out of a room it is not out
    of, and every link in that menu would bounce straight back to the code prompt.

    Takes the request rather than the user, because "is this session signed in" is a
    question about a request and asking it of a model instance is the mistake this
    signature makes impossible.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return False
    if not user.mfa_required:
        return True

    # Deliberately not _verification_check: this one is read while a template
    # renders, and a template renders in places a request was assembled by hand and
    # never passed through OTPMiddleware. An absent check still means "not
    # verified", but here that hides a menu where raising would break a page. The
    # middleware below is the half that would be failing to *gate*, so it is the
    # half that shouts — and on a real request it runs first anyway.
    verified = getattr(user, "is_verified", None)
    return bool(verified and verified())


class MFAEnforcementMiddleware:
    """Hold a session that owes a second factor at the enrolment step.

    Applies to anyone with ``mfa_required``, which is set from the role but
    stored, so it stays correct if a particular account is ever excepted.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)

        if user is None or not user.is_authenticated or not user.mfa_required:
            return self.get_response(request)

        if _verification_check(user)():
            return self.get_response(request)

        if self._is_exempt(request.path):
            return self.get_response(request)

        from apps.accounts.services import confirmed_device

        target = "accounts:mfa_verify" if confirmed_device(user) else "accounts:mfa_setup"
        return redirect(target)

    def _is_exempt(self, path):
        # Static assets are served before any view and carry nothing sensitive;
        # gating them would only break the styling of the enrolment page itself.
        if settings.STATIC_URL and path.startswith(f"/{settings.STATIC_URL.lstrip('/')}"):
            return True
        return path in {reverse(name) for name in MFA_EXEMPT_URL_NAMES}


class LastSeenMiddleware:
    """Keep ``User.last_seen_at`` roughly current.

    Used to answer "is this account still in use" before deactivating someone,
    and to make an abandoned account visible. Deliberately coarse: this is not
    the audit trail, and it must not cost a write on every request.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            now = timezone.now()
            if user.last_seen_at is None or now - user.last_seen_at > LAST_SEEN_INTERVAL:
                # Updated through the queryset rather than the instance so this
                # cannot trip a model save hook or clobber a field the view just
                # changed on its own copy of the user.
                #
                # get_user_model() rather than type(user): AuthenticationMiddleware
                # sets request.user to a SimpleLazyObject, whose *type* is the lazy
                # wrapper even though its __class__ proxies through to User. Asking
                # for the type here would raise AttributeError on every request.
                get_user_model().objects.filter(pk=user.pk).update(last_seen_at=now, updated_at=now)

        return response
