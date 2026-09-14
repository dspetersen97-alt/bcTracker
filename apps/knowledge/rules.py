"""
Object-level permissions for the knowledge base.

``ResourceQuerySet`` answers "may this appear on the shelf". These answer "may this
actor do this", which is what a write needs. Both apply, as everywhere: a view
resolves the row through ``for_actor`` *and* checks a permission.

Neither ``is_counselee`` nor ``is_financial_admin`` appears anywhere below. That is
the whole of the guarantee the feature was asked for — the knowledge base is for
counselors and administrators — and it is stated twice on purpose: here, so a write
is refused, and in the queryset, so a read finds nothing to refuse.
"""

import rules

from apps.accounts.rules import is_admin, is_counselor


@rules.predicate
def is_the_contributor(user, resource):
    if resource is None:
        return False
    return resource.contributed_by_id == user.pk


@rules.predicate
def is_the_comment_author(user, comment):
    if comment is None:
        return False
    return comment.author_id == user.pk


# Reading the shelf and opening anything on it. No object: it is one shelf for the
# whole ministry, so there is nothing to check a resource against.
rules.add_perm("knowledge.view_resources", is_admin | is_counselor)

# Contributing. Every counselor, not only administrators, and this is the line that
# separates this feature from the template library: an administrator decides which
# intake form is current, but nobody decides which handout a colleague found helpful.
rules.add_perm("knowledge.add_resource", is_admin | is_counselor)

# Relabelling and removing. The contributor may tidy up their own, and an
# administrator may tidy up anybody's — somebody has to be able to take down a dead
# link or a resource the ministry has moved away from, and that cannot wait for the
# counselor who added it four years ago to come back and do it.
rules.add_perm("knowledge.change_resource", is_admin | is_the_contributor)
rules.add_perm("knowledge.delete_resource", is_admin | is_the_contributor)

# Commenting. Checked against no object, because the resource has already been
# resolved through ``for_actor`` by the time a comment is being written: anybody who
# can read the shelf can annotate it, which is what makes it the ministry's shelf
# rather than a set of announcements.
rules.add_perm("knowledge.add_comment", is_admin | is_counselor)

# Removing a comment. The author, or an administrator. Deliberately *not* the
# resource's contributor: somebody who could delete the comments on their own
# contribution could quietly remove a colleague's "this one did not go well", and the
# comments are worth more than the contributor's feelings about them.
rules.add_perm("knowledge.delete_comment", is_admin | is_the_comment_author)
