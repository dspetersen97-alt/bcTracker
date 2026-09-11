"""
Role predicates, permissions, and the view mixins built on them.

apps/accounts/rules.py is the vocabulary the rest of the app composes against, so
the properties worth pinning down are the ones other apps will rely on: a
predicate is false for anonymous and for deactivated accounts, and a permission
granted to one role is denied to the other three.
"""

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory
from django.views.generic import View

from apps.accounts import rules as account_rules
from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.core.mixins import RoleRequiredMixin, StaffRoleRequiredMixin

pytestmark = pytest.mark.django_db

#: predicate -> the one role it should be true for.
ROLE_PREDICATES = {
    account_rules.is_admin: Role.ADMIN,
    account_rules.is_counselor: Role.COUNSELOR,
    account_rules.is_financial_admin: Role.FINANCIAL_ADMIN,
    account_rules.is_counselee: Role.COUNSELEE,
}


class TestPredicates:
    @pytest.mark.parametrize("predicate,role", list(ROLE_PREDICATES.items()))
    def test_each_predicate_matches_exactly_one_role(self, make_user, predicate, role):
        for candidate in Role:
            assert bool(predicate(make_user(candidate))) is (candidate == role)

    @pytest.mark.parametrize("predicate", list(ROLE_PREDICATES))
    def test_no_predicate_is_true_for_anonymous(self, predicate):
        assert not predicate(AnonymousUser())

    @pytest.mark.parametrize("predicate,role", list(ROLE_PREDICATES.items()))
    def test_deactivating_an_account_revokes_every_predicate(self, make_user, predicate, role):
        """Accounts are deactivated rather than deleted, so this is the off switch."""
        user = make_user(role)
        user.is_active = False

        assert not predicate(user)

    @pytest.mark.parametrize(
        "role,expected",
        [
            (Role.ADMIN, True),
            (Role.COUNSELOR, True),
            (Role.FINANCIAL_ADMIN, True),
            (Role.COUNSELEE, False),
        ],
    )
    def test_ministry_staff_is_everyone_but_the_counselee(self, make_user, role, expected):
        assert bool(account_rules.is_ministry_staff(make_user(role))) is expected

    def test_never_is_never_true(self, make_user):
        """Used to state a denial out loud where silence would look accidental."""
        for role in Role:
            assert not account_rules.never(make_user(role))


class TestPermissions:
    @pytest.mark.parametrize(
        "role,expected",
        [
            (Role.ADMIN, True),
            (Role.COUNSELOR, False),
            (Role.FINANCIAL_ADMIN, False),
            (Role.COUNSELEE, False),
        ],
    )
    def test_manage_users_belongs_to_the_admin_role(self, make_user, role, expected):
        assert make_user(role).has_perm("accounts.manage_users") is expected

    @pytest.mark.parametrize(
        "role,expected",
        [
            (Role.ADMIN, True),
            (Role.FINANCIAL_ADMIN, True),
            (Role.COUNSELOR, False),
            (Role.COUNSELEE, False),
        ],
    )
    def test_the_caseload_index_is_for_admin_and_billing(self, make_user, role, expected):
        """A counselor has no business seeing another counselor's caseload."""
        assert make_user(role).has_perm("accounts.view_caseload_index") is expected

    def test_an_unknown_permission_is_denied(self, admin_user):
        assert not admin_user.has_perm("accounts.not_a_real_permission")

    def test_the_break_glass_superuser_still_bypasses_everything(self, superuser):
        """Stated as a fact about the account, not an endorsement of using it."""
        assert superuser.has_perm("accounts.manage_users")


class TestRoleRequiredMixin:
    @staticmethod
    def make_view(roles):
        class Restricted(RoleRequiredMixin, View):
            allowed_roles = roles

            def get(self, request, *args, **kwargs):
                from django.http import HttpResponse

                return HttpResponse("ok")

        return Restricted.as_view()

    def request_as(self, user):
        request = RequestFactory().get("/somewhere/")
        request.user = user
        return request

    def test_a_permitted_role_gets_through(self, counselor):
        view = self.make_view((Role.COUNSELOR,))

        assert view(self.request_as(counselor)).status_code == 200

    def test_another_role_is_refused_with_403_not_a_login_redirect(self, counselee):
        """403 because the actor is known: sending them to log in again is a lie."""
        view = self.make_view((Role.COUNSELOR,))

        with pytest.raises(PermissionDenied):
            view(self.request_as(counselee))

    def test_a_refusal_is_audited(self, counselee):
        view = self.make_view((Role.COUNSELOR,))

        with pytest.raises(PermissionDenied):
            view(self.request_as(counselee))

        event = AuditEvent.objects.get(verb=AuditVerb.ACCESS_DENIED)
        assert event.actor == counselee
        assert event.metadata["reason"] == "role not permitted"

    def test_forgetting_to_set_allowed_roles_denies_everyone(self, admin_user):
        """The default must be closed; an empty tuple is not an accidental allow."""
        view = self.make_view(())

        with pytest.raises(PermissionDenied):
            view(self.request_as(admin_user))

    def test_anonymous_is_sent_to_log_in_rather_than_403(self):
        view = self.make_view((Role.COUNSELOR,))

        response = view(self.request_as(AnonymousUser()))

        assert response.status_code == 302
        assert "/login/" in response["Location"]

    @pytest.mark.parametrize(
        "role,allowed",
        [
            (Role.ADMIN, True),
            (Role.COUNSELOR, True),
            (Role.FINANCIAL_ADMIN, True),
            (Role.COUNSELEE, False),
        ],
    )
    def test_the_staff_variant_admits_the_three_staff_roles(self, make_user, role, allowed):
        class StaffOnly(StaffRoleRequiredMixin, View):
            def get(self, request, *args, **kwargs):
                from django.http import HttpResponse

                return HttpResponse("ok")

        view = StaffOnly.as_view()
        request = self.request_as(make_user(role))

        if allowed:
            assert view(request).status_code == 200
        else:
            with pytest.raises(PermissionDenied):
                view(request)
