"""
Object-level permissions for cases.

The queryset layer answers "may this row appear in a list". These answer "may
this actor do this to this row", which is the question writes ask. Both apply:
a view resolves the object through ``for_actor`` *and* checks the permission
before mutating it.

Composed from the role predicates in apps/accounts/rules.py rather than
re-deriving them, so there is one definition of what a counselor is.
"""

import rules

from apps.accounts.rules import is_admin, is_counselee, is_counselor, is_financial_admin


@rules.predicate
def is_case_counselor(user, case):
    """The counselor assigned to this specific case.

    Returns False when ``case`` is None. A predicate asked without an object is
    being used as a blanket check, and "may edit some case" is not a question
    this app wants to answer affirmatively.
    """
    if case is None:
        return False
    return case.counselor_id == user.pk


@rules.predicate
def is_case_member(user, case):
    if case is None:
        return False
    return case.members.filter(counselee=user, ended_on__isnull=True).exists()


@rules.predicate
def case_is_open(user, case):
    from apps.counseling.models import CaseStatus

    if case is None:
        return False
    return case.status != CaseStatus.CLOSED


# --- permissions ----------------------------------------------------------

# Reading a case. financial_admin is included deliberately: billing needs to know
# a case exists, who carries it, and who is on it. What it must never reach is the
# content — documents and messages both deny it at the queryset layer, and Case's
# own notes field is kept off every financial_admin template.
rules.add_perm(
    "counseling.view_case",
    is_admin | is_case_counselor | is_case_member | is_financial_admin,
)

# Editing the case record — label, kind, notes, status. The assigned counselor and
# an administrator. A counselee cannot edit the case they are the subject of.
rules.add_perm("counseling.change_case", is_admin | is_case_counselor)

# Only an administrator opens a case and assigns its counselor. Letting counselors
# create their own cases would mean the caseload is whatever each counselor
# decided, which is the opposite of what an admin-run ministry needs.
rules.add_perm("counseling.add_case", is_admin)

# Adding or removing a counselee. An administrator's job: it changes who can see
# what, so it is the one action in this app with a real disclosure consequence.
rules.add_perm("counseling.manage_case_members", is_admin)

# Closing a case. The counselor decides when counseling has concluded.
rules.add_perm("counseling.close_case", (is_admin | is_case_counselor) & case_is_open)

# Deleting a case is not a permission anyone holds. Cases are closed, and closed
# cases are retained — appointment and billing history point at them, and a
# counseling record is not something to make disappear. Stated here so its absence
# reads as a decision.
rules.add_perm("counseling.delete_case", rules.predicate(lambda user, case: False))

# A counselor's own practice settings: session length, notice, horizon. Only the
# counselor, not an administrator — these describe how one person works, and the
# view has no counselor id in its URL precisely so it cannot mean anyone else.
rules.add_perm("counseling.change_own_counselor_profile", is_counselor)

# The "view counselee" page: one person's sessions, documents, and notes on one
# screen. Staff only, and object-level scoping is left to
# ``CaseMember.objects.for_actor`` in the view — a counselor with no membership row
# for this person gets 404, which is the right answer because it does not confirm
# the account exists.
#
# ``financial_admin`` is absent, and that absence is the point of the page having
# its own permission: billing has the caseload index, which is names and counts.
# A counselee is absent too — what this page is for them is their own dashboard,
# and a route keyed on a person's id is not how anybody should reach their own.
rules.add_perm("counseling.view_counselee", is_admin | is_counselor)

# Intake details — date of birth, address, emergency contact. The counselee may
# maintain their own; their counselor and an admin may read and correct it.
# financial_admin is absent, matching CounseleeProfileQuerySet.
rules.add_perm("counseling.change_counselee_profile", is_admin | is_counselor | is_counselee)
