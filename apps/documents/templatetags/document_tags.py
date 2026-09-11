"""
Template helpers for documents.

There is one, and it exists because a template cannot be trusted with an access
decision. ``{{ document.owner.full_name }}`` is the obvious thing to write and it
names a counselee's spouse on a shared case, so the label is computed on the
model instead — see ``Document.source_label_for``.
"""

from django import template

register = template.Library()


@register.filter
def source_label(document, viewer) -> str:
    """Who to name as the source of ``document``, from ``viewer``'s side."""
    return document.source_label_for(viewer)
