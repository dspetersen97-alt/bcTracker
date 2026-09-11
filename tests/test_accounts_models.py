"""Tests for the user model and the role invariants it enforces."""

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import STAFF_ROLES, Role, User


@pytest.mark.django_db
class TestUserCreation:
    def test_email_is_normalised_and_required(self):
        user = User.objects.create_user(email="Person@Example.ORG", role=Role.COUNSELEE)
        # BaseUserManager lowercases the domain but preserves the local part,
        # which is technically correct: local parts are case-sensitive.
        assert user.email == "Person@example.org"

        with pytest.raises(ValueError):
            User.objects.create_user(email="", role=Role.COUNSELEE)

    def test_password_is_hashed_with_argon2(self, settings):
        # The test settings use MD5 for speed, so assert against the real
        # production hasher list instead of whatever is active.
        settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
        user = User.objects.create_user(
            email="hash@example.org", role=Role.COUNSELEE, password="a-real-password"
        )
        assert user.password.startswith("argon2$argon2id$")
        assert user.check_password("a-real-password")

    def test_user_without_password_cannot_authenticate(self):
        """Counselees who use magic links only should have no usable password."""
        user = User.objects.create_user(email="link@example.org", role=Role.COUNSELEE)
        assert not user.has_usable_password()
        assert not user.check_password("")


@pytest.mark.django_db
class TestRoleInvariants:
    @pytest.mark.parametrize("role", sorted(STAFF_ROLES))
    def test_staff_roles_require_mfa_by_default(self, role):
        user = User.objects.create_user(email=f"{role}@example.org", role=role)
        assert user.mfa_required is True
        assert user.is_ministry_staff is True

    def test_counselees_do_not_require_mfa(self):
        user = User.objects.create_user(email="c@example.org", role=Role.COUNSELEE)
        assert user.mfa_required is False
        assert user.is_ministry_staff is False

    @pytest.mark.parametrize("role", sorted(STAFF_ROLES))
    def test_database_rejects_magic_link_for_staff(self, role):
        """Passwordless email access is not an acceptable sole factor for staff.

        Asserted at the database level because the constraint is what makes this
        true regardless of which code path creates the account.
        """
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user(
                email=f"bad-{role}@example.org", role=role, allow_magic_link=True
            )

    def test_counselee_may_use_magic_link(self):
        user = User.objects.create_user(
            email="ok@example.org", role=Role.COUNSELEE, allow_magic_link=True
        )
        assert user.allow_magic_link is True

    def test_database_rejects_unknown_role(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="weird@example.org", role="pastor_emeritus")


@pytest.mark.django_db
class TestSuperuser:
    def test_superuser_is_separate_from_admin_role(self, superuser):
        """Being a ministry administrator must not imply bypassing scoping.

        The superuser flag is what grants admin access, and it is deliberately
        not set by creating an ADMIN-role user.
        """
        assert superuser.is_superuser is True

        ministry_admin = User.objects.create_user(
            email="ministry-admin@example.org", role=Role.ADMIN
        )
        assert ministry_admin.is_superuser is False
        assert ministry_admin.is_staff is False


@pytest.mark.django_db
class TestDisplayHelpers:
    def test_full_name_falls_back_to_email(self):
        user = User.objects.create_user(email="nameless@example.org", role=Role.COUNSELEE)
        assert user.full_name == "nameless@example.org"

    def test_full_name_uses_names_when_present(self):
        user = User.objects.create_user(
            email="named@example.org",
            role=Role.COUNSELEE,
            first_name="Ruth",
            last_name="Naomi",
        )
        assert user.full_name == "Ruth Naomi"
