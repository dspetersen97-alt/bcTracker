"""
Root URL configuration.

The admin is mounted at a configurable path and is restricted to superusers by
apps.core.admin_site — see the note there about why the admin is not the
management interface for any of the four product roles.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.core import views as core_views

urlpatterns = [
    path(f"{settings.ADMIN_URL_PATH}/", admin.site.urls),
    path("healthz", core_views.healthz, name="healthz"),
    # The home page every role lands on, and the ministry's own settings.
    path("", include("apps.core.urls")),
    # Signing in, the second factor, and the account page.
    path("", include("apps.accounts.urls")),
    # There is no MEDIA_URL. These are the only routes that reach the encrypted
    # document store, and each one resolves through Document.objects.for_actor.
    path("", include("apps.documents.urls")),
    # Office hours and appointments. The booking route is the one place a
    # counselee writes something a counselor's diary has to honour.
    path("", include("apps.scheduling.urls")),
    # Correspondence between a counselor and one counselee. Never the case: see
    # the note at the top of apps/messaging/models.py.
    path("", include("apps.messaging.urls")),
    # Sessions, invoices, and payments. Includes the Stripe webhook, which is the
    # only route in this application a stranger may reach — see the note in
    # apps/billing/stripe/webhook.py.
    path("", include("apps.billing.urls")),
    # Cases, caseloads and the role router at /dashboard/. Listed last by
    # convention rather than necessity: the site root is apps.core's home page.
    path("", include("apps.counseling.urls")),
]
