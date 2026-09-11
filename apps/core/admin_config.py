"""
Replacement for ``django.contrib.admin``'s AppConfig.

This lives outside ``apps.py`` on purpose: Django scans ``<app>/apps.py`` for a
default AppConfig, and having two candidates there is an error.
"""

from django.contrib.admin.apps import AdminConfig


class BcTrackerAdminConfig(AdminConfig):
    """Makes the superuser-only admin site the default one.

    Swapping the default site (rather than instantiating a second AdminSite)
    means ``admin.site`` and every plain ``@admin.register`` decorator are
    covered automatically — there is no unhardened site left to reach.
    """

    default_site = "apps.core.admin_site.BreakGlassAdminSite"
