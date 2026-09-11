"""
Role predicates, and the permissions built from them.

Autodiscovered by ``rules.apps.AutodiscoverRulesConfig``. These are the shared
vocabulary the other apps compose against — apps/documents/rules.py will say
"the case counselor, or an admin", not re-derive what a counselor is.

Why predicates at all, when the queryset layer already filters reads: filtering
answers "may this appear in a list", which is not the same question as "may this
be deleted". Writes need a check that names the action.
"""

import rules

from apps.accounts.models import Role


@rules.predicate
def is_authenticated(user):
    return bool(user and user.is_authenticated and user.is_active)


@rules.predicate
def is_admin(user):
    return is_authenticated(user) and user.role == Role.ADMIN


@rules.predicate
def is_counselor(user):
    return is_authenticated(user) and user.role == Role.COUNSELOR


@rules.predicate
def is_financial_admin(user):
    return is_authenticated(user) and user.role == Role.FINANCIAL_ADMIN


@rules.predicate
def is_counselee(user):
    return is_authenticated(user) and user.role == Role.COUNSELEE


@rules.predicate
def is_ministry_staff(user):
    return is_authenticated(user) and user.is_ministry_staff


#: Never granted to anyone. Used to state a denial explicitly where silence would
#: read as an oversight — financial_admin and documents, for instance.
never = rules.predicate(lambda user: False, name="never")


# --- permissions ----------------------------------------------------------

# Creating and deactivating accounts is the ministry administrator's job.
# Deliberately not granted to the superuser through this path: a superuser gets
# it from ModelBackend anyway, and that separation keeps the break-glass account
# visible as an exception rather than the normal way to work.
rules.add_perm("accounts.manage_users", is_admin)

# Seeing which counselees a counselor carries. Billing needs it; a counselor does
# not need to see anyone else's caseload, and a counselee never does.
rules.add_perm("accounts.view_caseload_index", is_admin | is_financial_admin)
