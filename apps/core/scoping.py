"""
The mandatory queryset scoping layer.

Every model holding counselee data must expose ``.for_actor(user)``, and views
must resolve objects only through it. The contract is deliberately fail-closed:

  * an unrecognised or inactive actor gets ``none()``, never everything;
  * a role that has not been explicitly granted access gets ``none()``, so
    adding a fifth role later denies by default rather than leaking;
  * subclasses declare access by overriding the small per-role hooks rather
    than rewriting the dispatch, so the deny-by-default branch cannot be
    accidentally removed.

Enforcement that this is actually used lives in the test suite: see
tests/test_access_matrix.py, which walks every route for every role, and
tests/test_scoping_contract.py, which fails if a case-linked model ships
without a ``for_actor``.
"""

from django.db import models


class ActorScopedQuerySet(models.QuerySet):
    """Base for querysets that must be narrowed to what one actor may see."""

    def for_actor(self, user):
        # Anonymous, missing, or deactivated actors see nothing. Checked before
        # any role dispatch so a disabled account cannot retain access.
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        if not user.is_active:
            return self.none()

        from apps.accounts.models import Role

        match user.role:
            case Role.ADMIN:
                return self.scope_for_admin(user)
            case Role.COUNSELOR:
                return self.scope_for_counselor(user)
            case Role.COUNSELEE:
                return self.scope_for_counselee(user)
            case Role.FINANCIAL_ADMIN:
                return self.scope_for_financial_admin(user)
            case _:
                # Unknown role: deny. This is the branch that makes a future
                # role addition safe by default.
                return self.none()

    # --- per-role hooks ---------------------------------------------------
    # Default to none() so a subclass must opt each role in explicitly.

    def scope_for_admin(self, user):
        return self.none()

    def scope_for_counselor(self, user):
        return self.none()

    def scope_for_counselee(self, user):
        return self.none()

    def scope_for_financial_admin(self, user):
        return self.none()


class CaseScopedQuerySet(ActorScopedQuerySet):
    """
    Scoping for models that reach a Case through a single relation.

    Subclasses set ``case_path`` to the ORM path from this model to its Case:
    ``"case"`` for a direct FK, ``"booking__case"`` for something one hop
    further out, or ``""`` for the Case model itself. All four roles then behave
    correctly without restating the rules.
    """

    case_path = "case"

    def _filter(self, **kwargs):
        # An empty case_path means this queryset *is* over Cases, so the lookups
        # apply directly rather than through a relation.
        prefix = f"{self.case_path}__" if self.case_path else ""
        return self.filter(**{f"{prefix}{k}": v for k, v in kwargs.items()})

    def for_actor(self, user):
        """Role scoping, then: nothing hanging off a withdrawn case.

        Done here rather than in each subclass so a model added later cannot
        forget it. Skipped when ``case_path`` is empty: on the Case model itself
        the default manager already hides soft-deleted rows, and ``all_objects``
        exists precisely so an administrator can still find one.
        """
        scoped = super().for_actor(user)
        if not self.case_path:
            return scoped
        return scoped.filter(**{f"{self.case_path}__deleted_at__isnull": True})

    def scope_for_admin(self, user):
        # Admin is the ministry's overseer role and can see every case.
        return self

    def scope_for_counselor(self, user):
        # Only cases this counselor is assigned to. A counselor must not be able
        # to learn that a case for someone else's counselee even exists.
        return self._filter(counselor=user)

    def scope_for_counselee(self, user):
        # Only cases the counselee is *currently* a member of. Matching on the
        # membership alone would leave access in place after it ended, which is
        # the opposite of what ending it means. Both lookups are in one filter()
        # call on purpose, so they must be satisfied by the same membership row.
        return self._filter(members__counselee=user, members__ended_on__isnull=True).distinct()

    def scope_for_financial_admin(self, user):
        # Billing needs to know which counselor serves which counselee, but not
        # what was discussed. Models carrying counseling content override this
        # back to none() — documents and messages both do.
        return self


class CaseScopedManager(models.Manager.from_queryset(CaseScopedQuerySet)):
    """Manager exposing ``for_actor`` at the model level."""

    def for_actor(self, user):
        return self.get_queryset().for_actor(user)
