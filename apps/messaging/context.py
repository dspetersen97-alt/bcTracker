"""
The unread count in the header.

One aggregate query per rendered page for a counselor or a counselee, and no
query at all for anyone else — see services.unread_total, which returns zero for
the roles with no place in a conversation without going near the database.

A context processor rather than a template tag because the header is in
base.html, which every page extends: a tag would have to be remembered by each
one, and the pages that forgot would silently stop telling somebody they have
mail.
"""

from apps.messaging.services import unread_total


def unread_messages(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    return {"unread_messages": unread_total(user)}
