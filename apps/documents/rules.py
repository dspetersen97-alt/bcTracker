"""
Object-level permissions for documents.

The queryset in models.py answers "may this appear in my list". These answer
"may this actor do this", which is what writes need. Both apply: every view
resolves the document through ``for_actor`` *and* checks a permission.

``financial_admin`` appears nowhere in this file. That is not an omission — the
predicates compose from the role predicates in apps/accounts/rules.py, and the
absence of ``is_financial_admin`` from every one of them, plus the ``none()`` in
DocumentQuerySet, is the whole of the guarantee that billing cannot reach a
counselee's file.
"""

import rules

from apps.accounts.rules import is_admin, is_counselee, is_counselor
from apps.counseling.rules import is_case_counselor, is_case_member


@rules.predicate
def is_document_owner(user, document):
    if document is None:
        return False
    return document.owner_id == user.pk


@rules.predicate
def is_counselor_on_the_documents_case(user, document):
    if document is None:
        return False
    return is_case_counselor(user, document.case)


@rules.predicate
def is_member_of_the_documents_case(user, document):
    if document is None:
        return False
    return is_case_member(user, document.case)


@rules.predicate
def is_shared_with_the_case(user, document):
    if document is None:
        return False
    return document.is_shared_with_the_case


# Reading a document — the metadata page and the download both. Deliberately
# reconstructs the queryset rule rather than trusting that the view scoped
# correctly: a counselee gets their own uploads and case-shared material, and a
# spouse's private upload satisfies neither branch.
rules.add_perm(
    "documents.view_document",
    is_admin
    | is_counselor_on_the_documents_case
    | is_document_owner
    | (is_member_of_the_documents_case & is_shared_with_the_case),
)

# The cross-case index — "my documents". No object to check against, so this is
# about the role alone, and the point of it is the role that is missing: without
# it financial_admin would reach the page and be shown an empty list, which is a
# statement about content rather than a refusal to discuss it.
rules.add_perm("documents.view_document_index", is_admin | is_counselor | is_counselee)

# Reaching the document list for a case at all. Checked against a *Case*.
# ``financial_admin`` can see the case itself — billing needs to know it exists —
# so without this they would reach the documents page and be shown an empty list,
# and an empty list is a claim about content. This makes it a 403 instead: the
# question of what is on the case is not one billing gets to ask.
rules.add_perm(
    "documents.view_case_documents",
    is_admin | is_case_counselor | is_case_member,
)

# Uploading. Checked against a *Case*, not a Document, since there is no document
# yet. The counselor and the current members of the case, plus an admin.
rules.add_perm("documents.add_document", is_admin | is_case_counselor | is_case_member)

# Editing the title, description, and kind. The uploader may relabel their own
# file; the counselor may relabel anything on their case.
rules.add_perm(
    "documents.change_document",
    is_admin | is_counselor_on_the_documents_case | is_document_owner,
)

# Sharing a document with everyone on the case. **Counselor and admin only**, and
# not the uploader: a counselee cannot publish their own file to a spouse, because
# on a family case they may not know who else is on it. Making this the
# counselor's decision is what keeps "nothing of the other's" from being
# something one counselee can waive on the other's behalf.
rules.add_perm(
    "documents.share_document",
    is_admin | is_counselor_on_the_documents_case,
)

# Deleting — soft, always. The uploader may withdraw what they uploaded; the
# counselor may remove anything on their case. Nobody gets a hard delete through
# the application at all.
rules.add_perm(
    "documents.delete_document",
    is_admin | is_counselor_on_the_documents_case | is_document_owner,
)
