"""
Object-level permissions for messaging.

The queryset in models.py answers "may this conversation appear in my list".
These answer "may this actor do this", which is what a write needs. Both apply:
every view resolves the thread through ``for_actor`` *and* checks a permission.

Two absences are decisions rather than omissions.

``is_financial_admin`` appears nowhere. Together with the ``none()`` in
``ThreadQuerySet`` that is the whole of the guarantee that billing cannot read a
counselee's correspondence.

``is_admin`` appears on the reading permissions and on none of the writing ones.
An administrator oversees the ministry and may need to read what was said on a
case — a complaint, a safeguarding concern, a counselor who has left. They may
not join the conversation. The audience of a thread is the counselor and one
counselee, and a third voice appearing in it is not something an administrator
can add on the counselee's behalf. This is the same line the session note draws.

Attachment permissions are at the bottom and are pure delegation to the thread's:
an attachment is part of what was said, so it has no audience of its own.
"""

import rules

from apps.accounts.rules import is_admin, is_counselee, is_counselor
from apps.counseling.rules import case_is_open, is_case_counselor, is_case_member


@rules.predicate
def is_counselor_on_the_threads_case(user, thread):
    if thread is None:
        return False
    return is_case_counselor(user, thread.case)


@rules.predicate
def is_member_of_the_threads_case(user, thread):
    if thread is None:
        return False
    return is_case_member(user, thread.case)


@rules.predicate
def is_in_the_conversation(user, thread):
    """Holds a participant row.

    Membership of the case is not enough and is checked separately: on a couple's
    case both spouses are members, and this is what keeps each of them out of the
    other's correspondence.
    """
    if thread is None:
        return False
    return thread.participants.filter(user=user).exists()


@rules.predicate
def thread_is_open(user, thread):
    if thread is None:
        return False
    return thread.is_open


@rules.predicate
def threads_case_is_open(user, thread):
    if thread is None:
        return False
    return case_is_open(user, thread.case)


#: The counselee half of every rule below: on the case now, and in this
#: conversation. Named because it is written four times and must not drift.
is_the_counselee_in_the_conversation = is_member_of_the_threads_case & is_in_the_conversation


# Reading a conversation. Reconstructs the queryset rule deliberately rather than
# trusting that the view scoped correctly.
rules.add_perm(
    "messaging.view_thread",
    is_admin | is_counselor_on_the_threads_case | is_the_counselee_in_the_conversation,
)

# The cross-case index — "my messages". No object, so this is about the role
# alone, and the point of it is the role that is missing: without it
# financial_admin would reach the page and be shown an empty list, and an empty
# list is a claim about content rather than a refusal to discuss it.
rules.add_perm("messaging.view_thread_index", is_admin | is_counselor | is_counselee)

# Reaching the conversations on a case at all. Checked against a *Case*. Same
# reasoning as documents.view_case_documents: financial_admin can see the case,
# so without this they would get an empty page instead of a refusal.
rules.add_perm(
    "messaging.view_case_threads",
    is_admin | is_case_counselor | is_case_member,
)

# Starting a conversation. Checked against a *Case*, since there is no thread
# yet. Either party to the counseling relationship may open one; an administrator
# may not, and neither party may start one on a closed case.
rules.add_perm("messaging.add_thread", (is_case_counselor | is_case_member) & case_is_open)

# Replying. The conversation must be open and so must the case: closing a thread
# is how a counselor says "not here", and closing a case is how the ministry says
# the counseling has concluded.
rules.add_perm(
    "messaging.add_message",
    (is_counselor_on_the_threads_case | is_the_counselee_in_the_conversation)
    & thread_is_open
    & threads_case_is_open,
)

# Closing and reopening a conversation are the counselor's, and only theirs. A
# counselee cannot close one — the thread is the channel their counselor reaches
# them on, and letting either party shut it would make "no reply" ambiguous.
rules.add_perm("messaging.close_thread", is_counselor_on_the_threads_case & thread_is_open)
rules.add_perm("messaging.reopen_thread", is_counselor_on_the_threads_case & ~thread_is_open)

# Deleting or editing a message is not a permission anyone holds, including an
# administrator and including the author. Stated here so its absence reads as a
# decision; enforced in Message.save, which refuses to write over a row.
rules.add_perm("messaging.change_message", rules.predicate(lambda user, message: False))
rules.add_perm("messaging.delete_message", rules.predicate(lambda user, message: False))


@rules.predicate
def may_read_the_attachments_thread(user, attachment):
    """Delegates, rather than restating the thread rule against an attachment.

    An attachment has no audience of its own — it is part of what was said — so
    every question about who may open one is really a question about the
    conversation. Written as a delegation so that a change to who may read a thread
    cannot leave a file behind, readable by somebody who can no longer read the
    message it came with.
    """
    if attachment is None:
        return False
    return user.has_perm("messaging.view_thread", attachment.message.thread)


rules.add_perm("messaging.view_attachment", may_read_the_attachments_thread)

# There is deliberately no ``add_attachment``. A file cannot be attached to a
# conversation without saying something — the form carries both — so sending one is
# ``messaging.add_message`` and inventing a second permission would create a state
# where the two could disagree about a closed thread.

# An attachment cannot be withdrawn, for the reason a message cannot be edited: it
# is part of what was said. A document can be soft-deleted because it is a filing
# cabinet; this is correspondence.
rules.add_perm("messaging.delete_attachment", rules.predicate(lambda user, attachment: False))
