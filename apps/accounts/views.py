"""
Authentication views.

Shape of the login flow, and why:

  * Password login logs the session in immediately, even for staff who still owe
    a TOTP code. The gate is ``MFAEnforcementMiddleware``, not this module, so a
    half-verified session can reach nothing regardless of which view is added
    next. Keeping the "am I verified" decision in one middleware is what makes
    that checkable; scattering it across views is how such rules rot.
  * Counselees may instead ask for an emailed link. Which addresses have accounts
    is never revealed — see ``apps.accounts.services.request_magic_link``.
  * Staff may instead sign in with a ministry Google account. It ends in the same
    ``auth_login`` as a password does, and therefore in the same TOTP gate — the
    Google assertion is not treated as a second factor. See ``apps.accounts.sso``.
  * Every outcome, including failures, is written to the audit trail.
"""

import io
import logging

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from django_otp import login as otp_login

from apps.accounts import sso
from apps.accounts.forms import (
    EmailAuthenticationForm,
    MagicLinkRequestForm,
    SetPasswordForm,
    TOTPCodeForm,
)
from apps.accounts.models import TokenPurpose
from apps.accounts.services import (
    LinkThrottled,
    confirm_device,
    confirmed_device,
    consume_token,
    request_magic_link,
    unconfirmed_device,
)
from apps.audit.models import AuditVerb
from apps.audit.services import record

logger = logging.getLogger(__name__)


def post_login_url(user) -> str:
    """Where a given role belongs after signing in.

    One function so the answer is stated once. The counseling dashboard is a
    router: it forwards each role to the page built for it, which keeps the
    per-role decision in the app that owns cases rather than here.
    """
    return reverse("counseling:dashboard")


def _safe_next(request, default):
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return default


# --- password login -------------------------------------------------------


class BcTrackerLoginView(LoginView):
    """Password login for every role."""

    template_name = "accounts/login.html"
    authentication_form = EmailAuthenticationForm
    redirect_authenticated_user = False  # see note in form_valid

    def form_valid(self, form):
        response = super().form_valid(form)
        user = form.get_user()
        record(
            AuditVerb.LOGIN_SUCCEEDED,
            actor=user,
            request=self.request,
            method="password",
            mfa_pending=user.mfa_required,
        )
        return response

    def form_invalid(self, form):
        # The submitted address is recorded, not the password. A run of these for
        # one address is the signal that something is being guessed at.
        record(
            AuditVerb.LOGIN_FAILED,
            request=self.request,
            method="password",
            email=form.data.get("username", "")[:254],
        )
        return super().form_invalid(form)

    def get_success_url(self):
        return _safe_next(self.request, post_login_url(self.request.user))

    def get_context_data(self, **kwargs):
        # The Google button is rendered only when it would work. A button that
        # reports "not available here" is worse than no button, particularly on the
        # page somebody reaches when they are already having trouble signing in.
        return super().get_context_data(**kwargs) | {"google_sso": sso.is_enabled()}


@require_POST
def logout_view(request):
    """End the session.

    POST only: a GET logout can be triggered by any page that embeds a link to
    it, which is a nuisance rather than a vulnerability but an avoidable one.
    """
    if request.user.is_authenticated:
        record(AuditVerb.LOGOUT, actor=request.user, request=request)
    auth_logout(request)
    messages.success(request, _("You have been signed out."))
    return redirect(settings.LOGOUT_REDIRECT_URL)


# --- Google sign-in for staff ---------------------------------------------


@require_POST
def google_login(request):
    """Send a staff member to Google's account chooser.

    POST, so a link in an email cannot start a handshake and mint a state token.
    ``sso.is_enabled()`` rather than a try/except: an unconfigured server should
    not be reachable here at all, because the button is not rendered.
    """
    if not sso.is_enabled():
        messages.error(request, _("Signing in with Google is not available here."))
        return redirect("accounts:login")
    return redirect(sso.start(request.session))


@never_cache
def google_login_callback(request):
    """Where Google sends the browser back.

    The order is the same as the calendar flow's and for the same reason: state
    first, then Google's reported error, then the exchange. Everything arriving
    here is attacker-controllable, so checking the state last would mean acting on
    a forged callback before noticing it was forged.

    A refusal is one message and one audit row. The message never says whether the
    address matched an account — see ``sso.SsoRefused``.
    """
    if not sso.is_enabled():
        return redirect("accounts:login")

    if not sso.state_is_valid(request.session, request.GET.get("state", "")):
        record(AuditVerb.LOGIN_FAILED, request=request, method="google", reason="state_mismatch")
        messages.error(request, _("That sign-in attempt has expired. Please try again."))
        return redirect("accounts:login")

    if request.GET.get("error") or not request.GET.get("code"):
        # Usually the account chooser being dismissed, which is not an error worth
        # a red banner.
        return redirect("accounts:login")

    claims = {}
    try:
        claims = sso.claims_for(request.GET["code"])
        user = sso.user_for(claims)
    except sso.SsoRefused as exc:
        record(
            AuditVerb.LOGIN_FAILED,
            request=request,
            method="google",
            reason=exc.reason,
            # The address is recorded because a run of these against one address is
            # the signal worth having. Empty when the exchange itself failed, since
            # there is nothing Google told us to record.
            email=str(claims.get("email", ""))[:254],
        )
        messages.error(request, str(exc))
        return redirect("accounts:login")

    # The session is logged in even though staff still owe a TOTP code, exactly as
    # after a password login. MFAEnforcementMiddleware is the gate; Google's
    # assertion is not accepted as a second factor — see apps/accounts/sso.py.
    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    record(AuditVerb.LOGIN_SUCCEEDED, actor=user, request=request, method="google")
    return redirect(post_login_url(user))


# --- magic links ----------------------------------------------------------


@never_cache
@require_http_methods(["GET", "POST"])
def magic_link_request(request):
    """Ask for an emailed sign-in link."""
    if request.user.is_authenticated:
        return redirect(post_login_url(request.user))

    form = MagicLinkRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            request_magic_link(email=form.cleaned_data["email"], request=request)
        except LinkThrottled:
            # Deliberately the same destination as success. Saying "too many
            # requests for this address" would confirm the address exists.
            logger.info("magic link request throttled")
        return redirect("accounts:magic_link_sent")

    return render(request, "accounts/magic_link_request.html", {"form": form})


@never_cache
def magic_link_sent(request):
    return render(request, "accounts/magic_link_sent.html")


@never_cache
@require_http_methods(["GET", "POST"])
def magic_link_consume(request, token):
    """Sign in from an emailed link.

    GET only offers a button; POST does the work. That is not ceremony: mail
    security scanners and inbox previewers fetch links, and a token spent by a
    GET would be gone before the counselee clicked it. Requiring a POST means a
    prefetch cannot consume the link.
    """
    if request.method == "GET":
        return render(request, "accounts/magic_link_confirm.html", {"token": token})

    user = consume_token(raw_token=token, purpose=TokenPurpose.MAGIC_LINK, request=request)
    if user is None:
        return render(request, "accounts/magic_link_invalid.html", status=400)

    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    record(AuditVerb.LOGIN_SUCCEEDED, actor=user, request=request, method="magic_link")
    return redirect(post_login_url(user))


# --- invitations ----------------------------------------------------------


@never_cache
@require_http_methods(["GET", "POST"])
def invitation_accept(request, token):
    """Set a first password from an invitation link.

    The token is checked but not spent on GET, so a scanner fetching the link
    does not lock the invitee out of their own account. It is spent by the POST
    that actually sets the password.
    """
    from apps.accounts.models import LoginToken

    candidate = LoginToken.objects.filter(
        token_hash=LoginToken.hash_token(token),
        purpose=TokenPurpose.INVITATION,
    ).first()
    if candidate is None or not candidate.is_usable or not candidate.user.is_active:
        return render(request, "accounts/invitation_invalid.html", status=400)

    form = SetPasswordForm(candidate.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        if consume_token(raw_token=token, purpose=TokenPurpose.INVITATION, request=request) is None:
            # Lost a race, or expired between the GET and the POST.
            return render(request, "accounts/invitation_invalid.html", status=400)
        # Log in the instance whose password was just set, not the equivalent one
        # consume_token loaded. Django derives the session's auth hash from the
        # password, so a stale copy produces a hash that no longer matches the
        # row — and the invitee is signed out again on their very next request.
        user = form.save()
        record(AuditVerb.PASSWORD_CHANGED, actor=user, request=request, method="invitation")
        auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        record(AuditVerb.LOGIN_SUCCEEDED, actor=user, request=request, method="invitation")
        messages.success(request, _("Your password is set. Welcome."))
        return redirect(post_login_url(user))

    return render(
        request,
        "accounts/invitation_accept.html",
        {"form": form, "token": token, "invitee": candidate.user},
    )


# --- second factor --------------------------------------------------------


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def mfa_setup(request):
    """Enrol an authenticator app.

    Refuses to run when the user already has a confirmed device. Otherwise a
    stolen but unverified session could enrol a *new* authenticator and satisfy
    the gate with a factor the attacker controls, which would make the second
    factor worth nothing.
    """
    if confirmed_device(request.user):
        return redirect("accounts:mfa_verify")

    device = unconfirmed_device(request.user, create=True)
    form = TOTPCodeForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        if device.verify_token(form.cleaned_data["code"]):
            confirm_device(device=device, user=request.user, request=request)
            otp_login(request, device)
            messages.success(request, _("Two-factor authentication is now active."))
            return redirect(_safe_next(request, post_login_url(request.user)))
        record(AuditVerb.MFA_FAILED, actor=request.user, request=request, stage="enrolment")
        form.add_error("code", _("That code was not accepted. Try the next one."))

    return render(
        request,
        "accounts/mfa_setup.html",
        {"form": form, "qr_svg": _qr_svg(device.config_url), "secret": device.key},
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def mfa_verify(request):
    """Enter a code from an already-enrolled authenticator."""
    device = confirmed_device(request.user)
    if device is None:
        return redirect("accounts:mfa_setup")

    if request.user.is_verified():
        return redirect(post_login_url(request.user))

    form = TOTPCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        # verify_token applies django-otp's own failure throttling, so repeated
        # wrong codes get progressively slower without us tracking attempts.
        if device.verify_token(form.cleaned_data["code"]):
            otp_login(request, device)
            record(AuditVerb.MFA_VERIFIED, actor=request.user, request=request)
            return redirect(_safe_next(request, post_login_url(request.user)))
        record(AuditVerb.MFA_FAILED, actor=request.user, request=request, stage="verification")
        form.add_error("code", _("That code was not accepted. Try the next one."))

    return render(request, "accounts/mfa_verify.html", {"form": form})


def _qr_svg(config_url: str) -> str:
    """Render the enrolment URI as inline SVG.

    SVG rather than a PNG data URI so the base qrcode install is enough and
    Pillow is not a dependency of logging in.
    """
    image = qrcode.make(config_url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode()


# --- account -------------------------------------------------------------


@login_required
@never_cache
def home(request):
    """The signed-in landing page.

    Every role lands here and sees only what its role implies. The role
    dashboards proper arrive with the counseling app; this is the shell they
    plug into, and it exists now so the access matrix has something to assert.
    """
    return render(
        request,
        "accounts/home.html",
        {
            "role_label": request.user.get_role_display(),
            "is_staff_role": request.user.is_ministry_staff,
            # Asked as a permission rather than as a role comparison, so the
            # answer comes from apps/accounts/rules.py and there is one place to
            # change if administration is ever delegated.
            "can_manage_users": request.user.has_perm("accounts.manage_users"),
        },
    )


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def password_change(request):
    form = SetPasswordForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        # Django cycles the session key so the new password does not leave old
        # sessions valid; without this, a session stolen before the change would
        # survive it.
        from django.contrib.auth import update_session_auth_hash

        update_session_auth_hash(request, request.user)
        record(AuditVerb.PASSWORD_CHANGED, actor=request.user, request=request, method="self")
        messages.success(request, _("Your password has been changed."))
        return HttpResponseRedirect(reverse_lazy("accounts:home"))
    return render(request, "accounts/password_change.html", {"form": form})
