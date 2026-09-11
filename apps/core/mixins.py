"""
View-side enforcement of the scoping layer.

apps/core/scoping.py decides *what* an actor may see. This module is how views
are made to ask. The important property is that a view cannot accidentally skip
it: ``ScopedObjectMixin`` overrides ``get_queryset`` rather than adding an extra
check next to it, so a subclass that forgets to call something still gets scoped
results.

A row an actor may not see produces 404, never 403. A counselor asking for
another counselor's case must not learn that the case exists — and "403" says it
does.
"""

from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import ImproperlyConfigured

from apps.accounts.models import STAFF_ROLES
from apps.audit.models import AuditVerb
from apps.audit.services import record


class ScopedQuerysetMixin:
    """Resolve every object through ``Model.objects.for_actor(request.user)``."""

    #: Set by subclasses, as with any Django generic view.
    model = None
    queryset = None

    def get_queryset(self):
        queryset = self.queryset
        if queryset is None:
            if self.model is None:
                raise ImproperlyConfigured(
                    f"{type(self).__name__} needs either 'model' or 'queryset'."
                )
            queryset = self.model._default_manager.all()

        for_actor = getattr(queryset, "for_actor", None)
        if for_actor is None:
            # Refusing to guess is the point. A model without for_actor has not
            # had its access rules written, and silently returning everything is
            # exactly the failure this layer exists to prevent.
            raise ImproperlyConfigured(
                f"{queryset.model.__name__} has no for_actor(); it cannot be used "
                "with ScopedQuerysetMixin. Give its manager an ActorScopedQuerySet."
            )
        return for_actor(self.request.user)


class RoleRequiredMixin(AccessMixin):
    """Restrict a view to a set of roles.

    Coarse by design: this answers "should this role see this *kind* of page at
    all", which is separate from "may this actor see this row". Both apply — the
    queryset scoping still runs.
    """

    #: Iterable of Role values. Empty means nobody, so a subclass that forgets to
    #: set it denies rather than admits.
    allowed_roles: tuple = ()

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return self.handle_no_permission()
        if user.role not in set(self.allowed_roles):
            record(
                AuditVerb.ACCESS_DENIED,
                actor=user,
                request=request,
                view=type(self).__name__,
                reason="role not permitted",
            )
            self.raise_exception = True  # 403, not a redirect back to login
            return self.handle_no_permission()
        return super().dispatch(request, *args, **kwargs)


class StaffRoleRequiredMixin(RoleRequiredMixin):
    """For pages belonging to ministry personnel rather than counselees."""

    allowed_roles = tuple(sorted(STAFF_ROLES))
