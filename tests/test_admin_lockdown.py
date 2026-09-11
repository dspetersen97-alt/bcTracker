"""
Tests that the Django admin cannot become a way around the access model.

The admin builds querysets straight off the default manager, so it bypasses the
scoping layer entirely. These tests are the tripwire: if someone later registers
Document in the admin, or relaxes the permission check, the suite fails.
"""

import pytest
from django.contrib import admin
from django.urls import reverse

from apps.core.admin_site import ADMIN_FORBIDDEN_MODELS, BreakGlassAdminSite


def test_default_admin_site_is_the_hardened_one():
    assert isinstance(admin.site, BreakGlassAdminSite)


def test_content_bearing_models_are_not_registered():
    """No admin path to counselee documents or messages may exist."""
    registered = {
        f"{model._meta.app_label}.{model._meta.object_name}" for model in admin.site._registry
    }
    leaked = registered & ADMIN_FORBIDDEN_MODELS
    assert not leaked, (
        f"These models must never be registered in the admin: {sorted(leaked)}. "
        "The admin cannot enforce per-case scoping or the financial_admin "
        "document restriction."
    )


@pytest.mark.django_db
class TestAdminAccess:
    """Signs actors in fully — through the login form and TOTP — rather than with
    force_login, so what these tests measure is the admin's own permission check
    and not the MFA gate in front of it. A half-verified session is already turned
    away at ``/admin/``; that is asserted in tests/test_auth_flows.py.
    """

    def _admin_index(self):
        return reverse("admin:index")

    @pytest.mark.parametrize(
        "fixture_name",
        ["admin_user", "counselor", "financial_admin", "counselee"],
    )
    def test_product_roles_cannot_reach_the_admin(self, client, request, sign_in, fixture_name):
        """None of the four product roles gets admin access.

        Including the product's own ADMIN role: ministry administrators get a
        purpose-built UI, because the admin cannot express "no documents for
        financial_admin" or "only assigned cases for a counselor".
        """
        sign_in(request.getfixturevalue(fixture_name))

        response = client.get(self._admin_index())

        # Django redirects an unauthorised user to the admin's own login page
        # rather than serving the index.
        assert response.status_code == 302
        assert reverse("admin:login") in response["Location"]

    def test_superuser_can_reach_the_admin(self, client, superuser, sign_in):
        sign_in(superuser)
        response = client.get(self._admin_index())
        assert response.status_code == 200

    def test_is_staff_alone_is_not_enough(self, client, counselor, sign_in):
        """Django's default check accepts is_staff; ours requires is_superuser."""
        counselor.is_staff = True
        counselor.save(update_fields=["is_staff"])
        sign_in(counselor)

        response = client.get(self._admin_index())
        assert response.status_code == 302
