"""
Template helpers for billing.

One filter, for the totals a template computes for itself — "outstanding across the
ministry", the sum of one counselee's unbilled sessions. An amount that belongs to a
row renders through the model's own ``display_`` method instead, so the currency comes
from the invoice rather than from settings and a bill raised before a currency change
still says what it said.
"""

from django import template

from apps.billing import money

register = template.Library()


@register.filter
def cents(value) -> str:
    """Whole cents as a printable amount: ``{{ total_cents|cents }}`` → ``$1,240.00``.

    Named for its input rather than for money in general, because the mistake this
    filter invites is applying it to an amount that is already in units — and
    ``{{ amount|cents }}`` reading oddly is the point.
    """
    try:
        return money.format_cents(int(value))
    except (TypeError, ValueError):
        # A template filter that raises takes the whole page down. An empty string is
        # visible as a missing amount, which is what a bug here deserves.
        return ""
