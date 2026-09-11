"""
Deploy-time checks for the Google Calendar integration.

The integration is optional, so none of this fires on a deployment that leaves it
switched off. What these refuse is a *half*-configured one — switched on with a
credential missing, or switched on without the hosted-domain restriction that is
the only thing standing between a counselor's slip of the finger and a ministry
calendar Google hosts for somebody else.

Registered in apps.py rather than written down in a deployment note, for the same
reason the document checks are: a note is not a control.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register


@register("scheduling", deploy=True)
def check_google_configuration(app_configs, **kwargs):
    if not settings.GOOGLE_CALENDAR_ENABLED:
        return []

    problems = []

    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        problems.append(
            Error(
                "GOOGLE_CALENDAR_ENABLED is on but the OAuth client is not configured, "
                "so every counselor who tries to connect a calendar gets an error.",
                hint=(
                    "Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET from the "
                    "Google Cloud console, or unset GOOGLE_CALENDAR_ENABLED."
                ),
                id="scheduling.E001",
            )
        )

    if not settings.GOOGLE_WORKSPACE_DOMAIN:
        # The strongest of these, and the least obvious. Without the `hd` claim to
        # check, a counselor who is signed into a personal account in the same
        # browser authorizes that one instead, and from then on every appointment
        # time — with names, if they switch names on — is written to a calendar the
        # ministry does not administer, cannot audit, and cannot revoke.
        problems.append(
            Error(
                "GOOGLE_WORKSPACE_DOMAIN is empty, so a counselor could connect a "
                "personal Google account and have ministry appointments written to a "
                "calendar the ministry does not control.",
                hint=(
                    "Set GOOGLE_WORKSPACE_DOMAIN to the ministry's Workspace domain. "
                    "It is checked against the hosted-domain claim Google returns."
                ),
                id="scheduling.E002",
            )
        )

    # SITE_BASE_URL has a default, so the failure mode is not "empty" but "still
    # the development default". The redirect URI is built from it and has to match
    # what is registered with Google exactly, so localhost means every connection
    # attempt dies at the consent screen with redirect_uri_mismatch.
    if "localhost" in settings.SITE_BASE_URL or "127.0.0.1" in settings.SITE_BASE_URL:
        problems.append(
            Error(
                f"SITE_BASE_URL is {settings.SITE_BASE_URL}, so the OAuth redirect URI "
                "will not match the one registered with Google and no counselor can "
                "finish connecting a calendar.",
                hint="Set SITE_BASE_URL to the ministry's real https:// address.",
                id="scheduling.E003",
            )
        )

    if settings.GOOGLE_EVENT_TITLE.strip() == "":
        problems.append(
            Warning(
                "GOOGLE_EVENT_TITLE is blank, so appointments appear untitled in a "
                "counselor's Google calendar.",
                id="scheduling.W001",
            )
        )

    return problems
