"""
Deploy-time checks for staff Google sign-in.

``sso.is_enabled()`` already refuses to run a half-configured flow, so nothing
here prevents an unsafe state — it makes a *silently disabled* one visible.
Without these, a ministry that set ``GOOGLE_SSO_ENABLED`` and forgot the domain
would find the button simply absent, with nothing anywhere saying why.
"""

from django.conf import settings
from django.core.checks import Error, register


@register("accounts", deploy=True)
def check_google_sso_configuration(app_configs, **kwargs):
    if not settings.GOOGLE_SSO_ENABLED:
        return []

    problems = []

    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        problems.append(
            Error(
                "GOOGLE_SSO_ENABLED is on but the OAuth client is not configured, so "
                "staff sign-in with Google is switched off in practice.",
                hint=(
                    "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET, and "
                    "register the redirect URI /login/google/callback/ in the Google "
                    "console."
                ),
                id="accounts.E001",
            )
        )

    if not settings.GOOGLE_WORKSPACE_DOMAIN:
        # An Error rather than a Warning even though the feature disables itself.
        # With no domain to check, "sign in with Google" would mean any Google
        # account whose address happens to match a staff address — an
        # authentication bypass rather than the laxer calendar rule — so this is
        # the one setting the flow refuses to run without.
        problems.append(
            Error(
                "GOOGLE_SSO_ENABLED is on but GOOGLE_WORKSPACE_DOMAIN is empty. Staff "
                "sign-in with Google stays switched off, because without the domain "
                "there is nothing restricting which Google account may sign in.",
                hint="Set GOOGLE_WORKSPACE_DOMAIN to the ministry's Workspace domain.",
                id="accounts.E002",
            )
        )

    return problems
