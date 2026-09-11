"""
Splitting the messages framework's queue by what is allowed to disappear.

``base.html`` shows a confirmation as a toast in the top right corner: it slides
in, waits a few seconds and fades out with nobody having to dismiss it. That is
right for "Case updated." — the page behind it already shows the change — and it
is wrong for anything the reader has to act on, because a message that removes
itself is a message somebody will miss. So warnings and errors keep the banner in
the flow of the page that every message used to get.

The split is here rather than in the template for two reasons: a template that
decided it would have to know the level constants, and it would decide it once per
container, leaving the two conditions free to drift apart until a message lands in
both places or in neither.

Levels, not tags. ``message.tags`` is a string that also carries whatever
``extra_tags`` the view passed, so matching on it would break the day somebody
writes ``messages.success(request, ..., extra_tags="warning-ish")``. The level is
the thing the framework itself orders by.
"""

from django import template
from django.contrib.messages import constants

register = template.Library()


@register.filter
def toasts(messages):
    """The messages that may fade on their own — confirmations and asides."""
    return [message for message in messages if message.level < constants.WARNING]


@register.filter
def banners(messages):
    """The messages that stay on the page until it is left.

    Anything at WARNING or above: a series that skipped two weeks, a calendar that
    refused a connection. The reader may have to do something about it, so it is
    not put anywhere that times out.
    """
    return [message for message in messages if message.level >= constants.WARNING]
