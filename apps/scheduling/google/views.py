"""
The counselor's Google calendar settings page, and the consent handshake.

Kept out of ``apps/scheduling/views.py`` because the OAuth round trip has
concerns nothing else in the app has — a state token in the session, a callback
from a third party, a redirect off-site — and mixing it in would put "did we
check the state parameter" in the middle of a file about booking appointments.

The permission is ``scheduling.manage_own_google_calendar``, which no admin
holds. There is no counselor id anywhere in these routes, so the views cannot be
pointed at another counselor's connection: ``request.user`` is the only subject
available.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.scheduling.forms import GoogleCalendarSettingsForm
from apps.scheduling.google import credentials, oauth, sync
from apps.scheduling.google.errors import GoogleError, GoogleNotConfigured
from apps.scheduling.models import GoogleCredential
from apps.scheduling.views import require_perm

logger = logging.getLogger(__name__)


def _own_credential(request) -> GoogleCredential | None:
    return GoogleCredential.objects.filter(counselor=request.user).first()


@login_required
@require_http_methods(["GET", "POST"])
def settings_page(request):
    """Connect, disconnect, and the three choices in between.

    POST edits the settings of an existing connection. Connecting and
    disconnecting are their own routes, because both leave the site or destroy a
    credential and neither should be reachable by a form that also toggles a
    checkbox.
    """
    from django.conf import settings as django_settings

    require_perm(request, "scheduling.manage_own_google_calendar")
    credential = _own_credential(request)

    form = None
    if credential is not None:
        form = GoogleCalendarSettingsForm(request.POST or None, instance=credential)
        if request.method == "POST" and form.is_valid():
            changed = form.changed_data
            form.save()
            record(
                AuditVerb.GOOGLE_SETTINGS_CHANGED,
                actor=request.user,
                target=credential,
                request=request,
                fields=sorted(changed),
                # Worth naming explicitly: switching names on changes what leaves
                # this system, so it should be findable in the trail without
                # reading a diff.
                include_names=credential.include_names,
            )
            messages.success(request, _("Saved."))
            return redirect("scheduling:google_settings")
    elif request.method == "POST":
        # Nothing to edit. Reached by a stale form after a disconnection in another
        # tab; a redirect is kinder than a 404 on a page they can see.
        return redirect("scheduling:google_settings")

    return render(
        request,
        "scheduling/google_settings.html",
        {
            "credential": credential,
            "form": form,
            "available": django_settings.GOOGLE_CALENDAR_ENABLED,
            "workspace_domain": django_settings.GOOGLE_WORKSPACE_DOMAIN,
            "shows_names": bool(credential and credential.include_names),
            # Shown verbatim, so the page can promise exactly what a Google event
            # will say rather than describing it approximately.
            "event_title": django_settings.GOOGLE_EVENT_TITLE,
        },
    )


@login_required
@require_POST
def connect(request):
    """Send the counselor to Google's consent screen.

    POST, not GET, and that is not pedantry: a GET would let a link in an email
    start an authorization handshake and mint a state token, which is the first
    half of the attack the state token exists to stop.
    """
    require_perm(request, "scheduling.manage_own_google_calendar")

    try:
        url = oauth.start(request.session)
    except GoogleNotConfigured as exc:
        logger.warning("Google connect attempted while unconfigured: %s", exc)
        messages.error(
            request,
            _("Google Calendar is not set up on this server. Please ask an administrator."),
        )
        return redirect("scheduling:google_settings")
    return redirect(url)


@login_required
def callback(request):
    """Where Google sends the browser back.

    GET, because Google chooses the method. Everything arriving here is attacker-
    controllable — the code, the state, the error — so the order matters: state
    first, then the error Google reported, then the exchange. Checking the state
    last would mean acting on a forged callback before noticing it was forged.
    """
    require_perm(request, "scheduling.manage_own_google_calendar")

    if not oauth.state_is_valid(request.session, request.GET.get("state", "")):
        # Deliberately vague to the user and specific in the trail. This is either a
        # CSRF attempt or a stale tab, and the counselor can do nothing about either
        # except start again.
        record(
            AuditVerb.GOOGLE_CONNECT_REFUSED,
            actor=request.user,
            request=request,
            reason="state_mismatch",
        )
        messages.error(request, _("That link had expired. Please try connecting again."))
        return redirect("scheduling:google_settings")

    if request.GET.get("error"):
        # Usually "access_denied" — the counselor changed their mind on the consent
        # screen, which is not an error worth a red banner.
        messages.info(request, _("Google Calendar was not connected."))
        return redirect("scheduling:google_settings")

    code = request.GET.get("code", "")
    if not code:
        messages.error(request, _("Google did not send an authorization code. Please try again."))
        return redirect("scheduling:google_settings")

    try:
        credential = oauth.finish(counselor=request.user, code=code)
    except GoogleError as exc:
        record(
            AuditVerb.GOOGLE_CONNECT_REFUSED,
            actor=request.user,
            request=request,
            reason=type(exc).__name__,
        )
        logger.warning("Google connection failed for counselor %s: %s", request.user.pk, exc)
        # The message from a DomainNotAllowed or a missing refresh token names what
        # to do about it, so it is shown; the taxonomy guarantees it carries no
        # token or client secret.
        messages.error(request, str(exc))
        return redirect("scheduling:google_settings")

    record(
        AuditVerb.GOOGLE_CONNECTED,
        actor=request.user,
        target=credential,
        request=request,
        google_email=credential.google_email,
        calendar_id=credential.calendar_id,
    )
    messages.success(
        request,
        _("Connected to %(email)s. Your upcoming appointments will appear there shortly.")
        % {"email": credential.google_email},
    )
    return redirect("scheduling:google_settings")


@login_required
@require_POST
def disconnect(request):
    """End the connection, at Google and here.

    Order matters the other way round from connecting: Google is asked first,
    because once the local ciphertext is gone there is no token left to revoke
    with. The local revocation happens regardless — a counselor clicking
    disconnect must not be left connected because Google was slow.
    """
    require_perm(request, "scheduling.manage_own_google_calendar")
    credential = _own_credential(request)
    if credential is None:
        return redirect("scheduling:google_settings")

    revoked_remotely = False
    if credential.is_usable:
        try:
            revoked_remotely = credentials.revoke_at_google(credential)
        except GoogleError as exc:
            logger.warning("Revoking at Google failed: %s", exc)

    # The row goes entirely. ``mark_revoked`` exists for a grant Google killed,
    # where the connection has to stay visible so the counselor can be asked to
    # reconnect; a deliberate disconnection has nothing to explain.
    #
    # The bookings keep their ``google_event_id``. Those events are still in the
    # counselor's calendar, and if they reconnect later the sync updates them
    # instead of inserting a second copy of every appointment.
    credential.delete()
    record(
        AuditVerb.GOOGLE_DISCONNECTED,
        actor=request.user,
        request=request,
        reason="counselor_request",
        revoked_at_google=revoked_remotely,
    )

    if revoked_remotely:
        messages.success(request, _("Disconnected. bcTracker no longer has access."))
    else:
        # Honest rather than reassuring. Our copy of the token is gone either way,
        # but if Google did not confirm, the counselor should know they can check.
        messages.success(
            request,
            _(
                "Disconnected, and bcTracker's copy of the access has been deleted. "
                "Google did not confirm the withdrawal, so you may also want to "
                "remove bcTracker under your Google account's security settings. "
                "Appointments already in your calendar are not removed."
            ),
        )
    return redirect("scheduling:google_settings")


@login_required
@require_POST
def resync(request):
    """Push everything now, instead of waiting for the next scheduled run.

    Worth a button: a counselor who has just fixed a connection wants to see their
    week appear, and "wait up to fifteen minutes" is the kind of answer that makes
    someone conclude the integration is broken.
    """
    require_perm(request, "scheduling.manage_own_google_calendar")
    credential = _own_credential(request)
    if credential is None or not credential.is_usable:
        messages.error(request, _("Connect a calendar first."))
        return redirect("scheduling:google_settings")

    tally = sync.sync_counselor(request.user)
    if tally.get("authorization_lost"):
        messages.error(request, _("Google refused the connection. Please reconnect."))
    elif tally["failed"]:
        messages.error(
            request,
            _("%(pushed)s appointment(s) updated, %(failed)s could not be. Please try again.")
            % tally,
        )
    else:
        messages.success(
            request,
            _("Up to date. %(pushed)s appointment(s) sent to Google.") % tally,
        )
    return redirect("scheduling:google_settings")
