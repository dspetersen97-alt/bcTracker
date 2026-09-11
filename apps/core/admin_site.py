"""
Admin hardening.

The Django admin bypasses the scoping layer in apps/core/scoping.py entirely: it
builds querysets straight off ``Model._default_manager``. That makes it the most
likely way the access model gets defeated, so:

  * admin access requires ``is_superuser`` — none of the four product roles
    (admin, counselor, financial_admin, counselee) can reach it, including the
    product's own "admin" role, which gets a purpose-built UI instead;
  * models carrying counseling content (Document, and later Message) are never
    registered here at all, so there is no admin path to a counselee's files;
  * tests/test_admin_lockdown.py asserts both of the above and fails if a future
    ``@admin.register`` reintroduces one.

The superuser account is a break-glass credential, not a day-to-day login.
"""

from django.contrib.admin import AdminSite

#: Models that must never be registered in the admin, because the admin cannot
#: enforce per-case scoping or the financial_admin document restriction.
ADMIN_FORBIDDEN_MODELS = frozenset(
    {
        "documents.Document",
        "messaging.Message",
        "messaging.MessageAttachment",
    }
)


class BreakGlassAdminSite(AdminSite):
    site_header = "bcTracker administration"
    site_title = "bcTracker"
    index_title = "Break-glass administration"

    def has_permission(self, request):
        # Deliberately stricter than Django's default, which only requires
        # is_active and is_staff. Staff-ness is not enough to bypass scoping.
        return request.user.is_active and request.user.is_superuser
