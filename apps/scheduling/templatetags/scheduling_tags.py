"""
Template helpers for scheduling.

One filter, for the same reason documents has one: ``{{ booking.counselee.full_name }}``
is the obvious thing to write and it names another member of a family case on a
joint appointment. The label is computed on the model instead — see
``Booking.attendee_label_for``.
"""

from django import template

register = template.Library()


@register.filter
def attendee_label(booking, viewer) -> str:
    """Whose appointment to say this is, from ``viewer``'s side."""
    return booking.attendee_label_for(viewer)
