"""
The home page, the deployment's own settings, and the operational probe.

Nothing here reads counselee data. The home page shows the signed-in person's own
name and the links their role has; the settings page is the ministry's mail
configuration. That is deliberate — the site root is the one page every role
lands on, so it is the page where a mistake would be seen by everybody at once.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core import mail
from apps.core.forms import MailSettingsForm
from apps.core.models import MailSettings

logger = logging.getLogger(__name__)


@never_cache
def healthz(request):
    """Liveness/readiness probe for the container healthcheck.

    Checks the database round-trip, because a web process that cannot reach
    Postgres is not usable. Intentionally returns no version or configuration
    detail — this endpoint is reachable without authentication.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


def require_perm(request, perm, obj=None):
    if not request.user.has_perm(perm, obj):
        record(
            AuditVerb.ACCESS_DENIED,
            actor=request.user,
            target=obj,
            request=request,
            permission=perm,
        )
        raise PermissionDenied
    return True


@login_required
def home(request):
    """Where everybody lands after signing in.

    The buttons come from ``nav_links``, which the context processor already put
    in the template context — the same list the sidebar renders, so this page
    cannot fall behind it. See apps/core/navigation.py.

    The mail warning is shown only to whoever can act on it. A counselee being
    told the ministry's SMTP is misconfigured learns nothing they can use and
    something about the ministry they did not need to know.
    """
    can_configure = request.user.has_perm("core.manage_site_settings")
    return render(
        request,
        "core/home.html",
        {
            "mail_warning": mail.unconfigured_reason() if can_configure else "",
            "can_configure_mail": can_configure,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def mail_settings(request):
    """Configure outgoing mail from the web UI.

    Exists because the alternative was editing ``.env`` on the host and
    restarting the container, which the person who discovers mail is broken
    generally cannot do. The password is sealed under the ministry's master key
    on the way in and is never rendered back — see apps/core/mail.py.
    """
    require_perm(request, "core.manage_site_settings")

    row = MailSettings.load()
    form = MailSettingsForm(request.POST or None, instance=row)
    if request.method == "POST" and form.is_valid():
        row = form.save(actor=request.user)
        record(
            AuditVerb.MAIL_SETTINGS_UPDATED,
            actor=request.user,
            target=row,
            request=request,
            # The values themselves, minus the secret: the host and the From
            # address are the part worth being able to review later, and the
            # password is the part that must never be in a second table.
            fields=sorted(form.changed_data),
            host=row.host,
            from_email=row.from_email,
        )
        messages.success(request, _("Email settings saved."))
        return redirect("core:mail_settings")

    return render(
        request,
        "core/mail_settings.html",
        {
            "form": form,
            "row": row,
            "unconfigured_reason": mail.unconfigured_reason(),
            "test_recipient": request.user.email,
        },
    )


@login_required
@require_POST
def mail_test(request):
    """Send one message to the signed-in administrator's own address.

    To their own address and nowhere else: a "send a test to…" box on a page
    behind one role is a small open relay, and the person who needs to know
    whether mail works is the person pressing the button.
    """
    require_perm(request, "core.manage_site_settings")

    try:
        mail.send_test_message(recipient=request.user.email, actor=request.user)
    except Exception as exc:
        # Broad on purpose: SMTP raises a family of errors, DNS raises something
        # else again, and the administrator needs the provider's own words rather
        # than a category. Logged as well, because the string is truncated on the
        # row that stores it.
        logger.warning("Mail test failed: %s", exc)
        messages.error(
            request,
            _("The test message could not be sent. %(error)s") % {"error": exc},
        )
    else:
        record(AuditVerb.MAIL_TEST_SENT, actor=request.user, request=request)
        messages.success(
            request,
            _("A test message is on its way to %(email)s.") % {"email": request.user.email},
        )
    return redirect("core:mail_settings")
