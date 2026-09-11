"""
Message notifications.

The rules are the ones apps/scheduling/notify.py sets out, and one more.

**Nothing of the conversation goes in the email.** Not the body, not the subject
line of the thread, not the name of the case. The mail says that something is
waiting and where to sign in to read it — that is the whole of it. Mail leaves our
control the moment it is sent, and a subject line reading "Re: the argument on
Sunday" is a disclosure to anyone who glances at a phone.

**A failed send never loses a message.** Every send here logs and swallows. The
message is already stored and visible in the application; an SMTP failure must
not roll that back.

**One nudge per conversation until it is read.** If somebody already has unread
messages in a thread, a second email tells them nothing they have not been told,
and a counselor writing three paragraphs in three messages should not produce
three emails. The check is on what was unread *before* this message, so the first
one after a read always goes out.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from apps.messaging.models import NEVER_READ

logger = logging.getLogger(__name__)


def _send(*, template, subject, recipient, context) -> None:
    try:
        send_mail(
            subject=subject,
            message=render_to_string(f"messaging/email/{template}.txt", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient.email],
            fail_silently=False,
        )
    except Exception:
        # Deliberately broad, as in scheduling.notify: SMTP raises a family of
        # errors and a DNS failure raises something else again, and none of them
        # is a reason to fail the message that triggered this.
        logger.exception("Could not send %s to user %s", template, recipient.pk)
    else:
        logger.info("sent %s to user %s", template, recipient.pk)


def _to_be_told(message):
    """Everyone in the conversation who should hear about this message.

    Not the author, not a deactivated account, and not somebody who is already
    sitting on something unread here.
    """
    participants = (
        message.thread.participants.exclude(user=message.author)
        .filter(user__is_active=True)
        .select_related("user")
    )
    for participant in participants:
        already_waiting = (
            message.thread.messages.filter(created_at__gt=participant.last_read_at or NEVER_READ)
            .exclude(author=participant.user)
            .exclude(pk=message.pk)
            .exists()
        )
        if not already_waiting:
            yield participant.user


def new_message(message) -> None:
    for recipient in _to_be_told(message):
        _send(
            template="new_message",
            # No thread subject, no name, no case. "A message" is the most this
            # can say without saying something about a counselee.
            subject="You have a new message",
            recipient=recipient,
            context={
                "recipient": recipient,
                # A link to the conversation is safe: reaching it needs a session,
                # and the URL carries a thread id rather than anything readable.
                # Built from SITE_BASE_URL, never from the request — a spoofed
                # Host header must not decide where a counselee is sent to sign in.
                "url": f"{settings.SITE_BASE_URL}/messages/{message.thread_id}/",
                "site_url": settings.SITE_BASE_URL,
                "from_staff": message.author.is_ministry_staff,
            },
        )
