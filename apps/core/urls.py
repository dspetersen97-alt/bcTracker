"""
The site's own routes: the home page and the deployment's settings.

Every route is named, because tests/test_access_matrix.py enumerates the URLconf
by name and fails if a route is missing from its matrix.

The home page claims ``""``. The role router that used to live there is now at
``/dashboard/`` — the site root is a page rather than a redirect, so that every
role has somewhere to land that tells them what they can do.
"""

from django.urls import path

from apps.core import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    # The mail configuration. Two routes rather than one, because sending a test
    # message is not saving a form: it must not be what a reload repeats.
    path("settings/email/", views.mail_settings, name="mail_settings"),
    path("settings/email/test/", views.mail_test, name="mail_test"),
]
