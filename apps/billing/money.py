"""
Money, as integers.

Every amount in this app is a whole number of cents stored in an ``IntegerField``.
Not ``Decimal``, and emphatically not ``float``:

  * A ``FloatField`` cannot represent 0.10 exactly, so a fee schedule of $85.10
    invoiced twelve times comes to $1021.1999999999999 and the total on the page
    disagrees with the sum of the lines by a cent that nobody can find.
  * ``DecimalField`` is exact and would work, but it has a scale, and every
    arithmetic operation is then a question about rounding — ``quantize`` with
    which rule, at which point. Cents have no scale to get wrong: a line total is
    ``quantity * unit_amount_cents`` and it is exact by construction, which is why
    a database CheckConstraint can assert it.
  * Stripe's API is denominated in the smallest currency unit as well, so an
    integer here goes over the wire unchanged. Converting on the boundary is one
    of the few places a rounding bug turns into a real charge.

So the ``Decimal`` conversions below exist only at the two edges — a staff member
typing "85.00" into a form, and a page printing "$85.00" — and nothing between
them ever holds a fractional cent.

One currency, named in ``settings.BILLING_CURRENCY``. See the note on that setting.
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings

#: Cents in a unit. Named rather than written as 100 in six places, and stated as
#: a constant so that the assumption is visible: this is wrong for a zero-decimal
#: currency such as JPY, where Stripe's smallest unit *is* the yen. A ministry
#: billing in one of those would change this and the two functions below, which is
#: exactly why they are in one module.
CENTS_PER_UNIT = 100

#: Symbols for the currencies a ministry is plausibly billing in. A currency that
#: is not here falls back to its code — "CHF 85.00" — which is correct if plain,
#: and much better than guessing a symbol.
SYMBOLS = {
    "usd": "$",
    "cad": "$",
    "aud": "$",
    "gbp": "£",
    "eur": "€",
}


def currency_code() -> str:
    return (settings.BILLING_CURRENCY or "usd").lower()


def to_cents(amount) -> int:
    """A form's decimal into whole cents.

    Raises ``ValueError`` on anything that is not a number, so a caller cannot
    quietly store zero. Rounds half-up, which is what somebody typing an amount
    expects and which only ever applies to input with more than two decimal
    places — an amount nobody meant to type.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{amount!r} is not an amount of money") from exc
    # Multiply before rounding, so 85.005 becomes 8501 rather than 8500 — the
    # rounding happens once, on the cent, not twice.
    return int((value * CENTS_PER_UNIT).to_integral_value(rounding="ROUND_HALF_UP"))


def from_cents(cents: int) -> Decimal:
    """Whole cents back into a decimal, for a form field's initial value."""
    return Decimal(int(cents)) / CENTS_PER_UNIT


def format_cents(cents: int, *, currency=None) -> str:
    """What a page prints. ``-$12.50`` for a negative, not ``$-12.50``."""
    code = (currency or currency_code()).lower()
    symbol = SYMBOLS.get(code)
    units, remainder = divmod(abs(int(cents)), CENTS_PER_UNIT)
    sign = "-" if cents < 0 else ""
    body = f"{units:,}.{remainder:02d}"
    if symbol:
        return f"{sign}{symbol}{body}"
    return f"{sign}{code.upper()} {body}"
