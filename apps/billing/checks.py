"""
Deploy-time checks for billing and the Stripe connection.

Stripe is optional, exactly as the calendar is: a ministry that takes cash and
checks runs this application with ``STRIPE_ENABLED`` off and loses nothing but the
card button. So none of these fires on that deployment.

What they refuse is a **half**-configured one, and one of them is the most
important check in the project. A webhook endpoint with no signing secret is an
unauthenticated "mark this invoice paid" route open to the internet — anybody who
guesses the URL can clear a counselee's balance. ``webhook.py`` refuses every
request when the secret is empty, and this makes the container say so at deploy
time rather than leaving it to be discovered from the log.

Registered in apps.py rather than written down in a deployment note, for the reason
the scheduling checks are: a note is not a control.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register


@register("billing", deploy=True)
def check_billing_configuration(app_configs, **kwargs):
    problems = []

    if settings.BILLING_DUE_DAYS < 0:
        problems.append(
            Error(
                f"BILLING_DUE_DAYS is {settings.BILLING_DUE_DAYS}, so every invoice "
                "would be issued already overdue.",
                hint="Set it to the number of days a counselee has to pay, e.g. 30.",
                id="billing.E001",
            )
        )

    if not settings.BILLING_CURRENCY or len(settings.BILLING_CURRENCY) != 3:
        problems.append(
            Error(
                f"BILLING_CURRENCY is {settings.BILLING_CURRENCY!r}, which is not a "
                "three-letter ISO currency code, so Stripe will refuse every charge.",
                hint="Set it to e.g. usd.",
                id="billing.E002",
            )
        )

    return problems


@register("billing", deploy=True)
def check_stripe_configuration(app_configs, **kwargs):
    if not settings.STRIPE_ENABLED:
        return []

    problems = []

    if not settings.STRIPE_SECRET_KEY:
        problems.append(
            Error(
                "STRIPE_ENABLED is on but STRIPE_SECRET_KEY is empty, so every "
                "counselee who tries to pay by card gets an error.",
                hint=(
                    "Set STRIPE_SECRET_KEY from the Stripe dashboard, or unset "
                    "STRIPE_ENABLED and record payments by hand."
                ),
                id="billing.E003",
            )
        )

    if not settings.STRIPE_WEBHOOK_SECRET:
        # The strongest of these. Without a signing secret there is nothing to
        # distinguish Stripe's "this invoice was paid" from anybody else's, and the
        # endpoint's whole job is to change a balance.
        problems.append(
            Error(
                "STRIPE_ENABLED is on but STRIPE_WEBHOOK_SECRET is empty. The "
                "webhook endpoint cannot verify that a request came from Stripe, so "
                "it refuses all of them and no card payment will ever be recorded.",
                hint=(
                    "Create an endpoint for /billing/stripe/webhook/ in the Stripe "
                    "dashboard and set STRIPE_WEBHOOK_SECRET to the whsec_... value "
                    "it gives you."
                ),
                id="billing.E004",
            )
        )

    if settings.STRIPE_SECRET_KEY.startswith("sk_test_"):
        problems.append(
            Warning(
                "STRIPE_SECRET_KEY is a test key, so cards will appear to be charged "
                "and no money will move.",
                hint="Use the live key (sk_live_...) in production.",
                id="billing.W001",
            )
        )

    # The Checkout success and cancel URLs are built from SITE_BASE_URL, and a
    # counselee is sent to them by Stripe rather than by us — so localhost here does
    # not fail at deploy, it fails after the card has been charged, with the payer
    # looking at a browser error and no way to tell whether they have paid.
    if "localhost" in settings.SITE_BASE_URL or "127.0.0.1" in settings.SITE_BASE_URL:
        problems.append(
            Error(
                f"SITE_BASE_URL is {settings.SITE_BASE_URL}, so after paying, a "
                "counselee would be returned to an address their browser cannot "
                "reach and would not know whether the payment succeeded.",
                hint="Set SITE_BASE_URL to the ministry's real https:// address.",
                id="billing.E005",
            )
        )

    if not 0 < settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS <= 900:
        problems.append(
            Warning(
                "STRIPE_WEBHOOK_TOLERANCE_SECONDS is "
                f"{settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS}. A large window lets a "
                "captured webhook be replayed long after the fact; a tiny one drops "
                "deliveries whenever the server clock drifts.",
                hint="Stripe's own default is 300 seconds.",
                id="billing.W002",
            )
        )

    return problems
