"""
The scoping contract.

apps/core/scoping.py is only a guarantee if every model that holds data about a
person actually uses it. That cannot be enforced by a base class — a developer can
always write a plain ``models.Manager`` — so it is enforced here instead: every
model of ours that reaches a ``Case`` or names a ``User`` must expose
``for_actor`` on its default manager, or appear in ``UNSCOPED_BY_DESIGN`` with a
reason.

The test finds those models by inspecting relations rather than by keeping a list,
because a list is exactly the thing that goes stale the week after it is written.
"""

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import RequestFactory

from apps.accounts.models import Role, User
from apps.core.mixins import ScopedQuerysetMixin
from apps.core.scoping import ActorScopedQuerySet, CaseScopedQuerySet

#: Models that need no scoping, each with a reason. Everything else our apps
#: define and that touches a person has to have for_actor.
UNSCOPED_BY_DESIGN = {
    # An audit row describes an action rather than a person, and its whole point
    # is that it is not filtered by who is asking. It is append-only and read
    # only through purpose-built reporting, never listed in a counselee view.
    "audit.AuditEvent",
    # Looked up by digest, never listed. A scoped queryset would not help: the
    # security property is that the digest is unguessable.
    "accounts.LoginToken",
    # One row, describing the deployment rather than a person. It is only in reach
    # of this test at all because it records which administrator last changed it.
    # Access to it is a single permission — core.manage_site_settings — and the
    # secret it holds is sealed, so a scoped queryset would protect nothing.
    "core.MailSettings",
}


def our_models():
    """Concrete models from this project's own apps.

    Third-party tables (django_otp devices, admin log entries) are excluded: we
    do not control their managers, and none of them is reachable from a
    counselee-facing view.
    """
    return [
        model
        for model in apps.get_models()
        if not model._meta.abstract
        and not model._meta.proxy
        and model._meta.app_config.name.startswith("apps.")
    ]


def forward_relations(model):
    for field in model._meta.get_fields():
        if isinstance(field, models.ForeignKey | models.OneToOneField):
            yield field.related_model


def reaches_case(model, *, seen=None):
    """True if this model has a forward FK/O2O path to ``counseling.Case``.

    Forward relations only: a model Case points *at* is not thereby holding case
    data. The ``seen`` set stops FK cycles.
    """
    try:
        case_model = apps.get_model("counseling", "Case")
    except LookupError:
        # The counseling app has not landed yet. Nothing can be case-linked, so
        # the check passes vacuously rather than failing the suite.
        return False

    seen = (seen or set()) | {model}
    for related in forward_relations(model):
        if related is case_model:
            return True
        if related not in seen and reaches_case(related, seen=seen):
            return True
    return False


def needs_scoping(model):
    """Whether this model holds data about a person, and so must be scoped.

    Two triggers, because a row can be about someone either by naming their case
    or by naming them. ``CounseleeProfile`` is the reason the second is needed —
    a date of birth and an emergency contact reach no Case at all.
    """
    if model is User:
        # The register of people. A counselor must not be able to list another
        # counselor's counselees.
        return True
    return reaches_case(model) or User in set(forward_relations(model))


class TestEveryModelAboutAPersonIsScoped:
    def test_they_expose_for_actor(self):
        offenders = sorted(
            model._meta.label
            for model in our_models()
            if needs_scoping(model)
            and model._meta.label not in UNSCOPED_BY_DESIGN
            and not hasattr(model._default_manager, "for_actor")
        )
        assert not offenders, (
            "These models hold data about a person but have no for_actor() on "
            "their default manager, so nothing stops a view listing every "
            f"counselee's rows: {offenders}. Give them an ActorScopedQuerySet, or "
            "add them to UNSCOPED_BY_DESIGN with a reason."
        )

    def test_the_check_actually_catches_something(self):
        """Guards against the check silently matching nothing.

        A refactor that broke ``needs_scoping`` would otherwise turn the test
        above into a no-op that passes forever.
        """
        flagged = {model._meta.label for model in our_models() if needs_scoping(model)}
        assert {"counseling.Case", "counseling.CaseMember", "accounts.User"} <= flagged
        assert "counseling.CounseleeProfile" in flagged, (
            "a profile reaches no Case, so the FK-to-User trigger is what covers it"
        )

    def test_the_exemption_list_names_real_models(self):
        """A stale exemption would silently excuse a model that needs scoping."""
        labels = {model._meta.label for model in our_models()}
        assert UNSCOPED_BY_DESIGN <= labels, sorted(UNSCOPED_BY_DESIGN - labels)


class TestFailClosedDefaults:
    """The scoping base must deny, not admit, when nobody has said otherwise."""

    def test_the_base_queryset_denies_every_role(self, make_user, db):
        class Bare(ActorScopedQuerySet):
            pass

        queryset = Bare(model=User)

        for role in Role:
            assert not queryset.for_actor(make_user(role)).exists(), (
                f"{role} got rows from a queryset that never granted access"
            )

    def test_an_anonymous_actor_gets_nothing(self, db):
        from django.contrib.auth.models import AnonymousUser

        assert not ActorScopedQuerySet(model=User).for_actor(AnonymousUser()).exists()
        assert not ActorScopedQuerySet(model=User).for_actor(None).exists()

    def test_a_deactivated_user_gets_nothing(self, counselor, db):
        """Checked before any role dispatch, so disabling an account is enough."""

        class Everything(CaseScopedQuerySet):
            case_path = ""

        counselor.is_active = False

        assert not Everything(model=User).for_actor(counselor).exists()

    def test_an_unrecognised_role_gets_nothing(self, counselor, db):
        """The branch that makes adding a fifth role safe by default."""

        class Everything(CaseScopedQuerySet):
            case_path = ""

        counselor.role = "auditor"  # a role that does not exist yet

        assert not Everything(model=User).for_actor(counselor).exists()


class TestScopedQuerysetMixin:
    def test_it_refuses_a_model_without_for_actor(self, counselor):
        """Refusing loudly beats returning everything."""
        from tests.testapp.models import Widget

        class Careless(ScopedQuerysetMixin):
            model = Widget

        view = Careless()
        view.request = RequestFactory().get("/")
        view.request.user = counselor

        with pytest.raises(ImproperlyConfigured, match="has no for_actor"):
            view.get_queryset()

    def test_it_requires_a_model_or_queryset(self):
        class Empty(ScopedQuerysetMixin):
            pass

        view = Empty()
        view.request = RequestFactory().get("/")

        with pytest.raises(ImproperlyConfigured, match="needs either"):
            view.get_queryset()

    def test_it_narrows_through_for_actor(self, counselor, db):
        """The mixin must call for_actor, not merely have it available."""
        calls = []

        class Recording(models.QuerySet):
            def for_actor(self, user):
                calls.append(user)
                return self.none()

        class View(ScopedQuerysetMixin):
            queryset = Recording(model=User)

        view = View()
        view.request = RequestFactory().get("/")
        view.request.user = counselor

        assert not view.get_queryset().exists()
        assert calls == [counselor]
