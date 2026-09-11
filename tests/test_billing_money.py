"""
Cents, formatting, and invoice numbering.

Small, fast, and worth having its own file, because these are the functions every
amount in the application passes through. A bug in ``to_cents`` is a bug in every
invoice ever raised, and it would not show up as an error — it would show up as a
ministry that quietly charged the wrong number.

The numbering tests are here rather than with the invoice lifecycle because what
they assert is a property of the Postgres sequence and not of an invoice: uniqueness
under concurrency, and a gap being acceptable. See apps/billing/numbering.py.
"""

from decimal import Decimal

import pytest
from django.db import connection

from apps.billing import money
from apps.billing.numbering import SEQUENCE_NAME, next_invoice_number


class TestToCents:
    @pytest.mark.parametrize(
        ("typed", "cents"),
        [
            ("85", 8500),
            ("85.00", 8500),
            ("85.5", 8550),
            ("0.01", 1),
            ("0", 0),
            ("1234.56", 123456),
            (Decimal("85.00"), 8500),
            (85, 8500),
        ],
    )
    def test_what_somebody_types_becomes_whole_cents(self, typed, cents):
        assert money.to_cents(typed) == cents

    def test_a_third_decimal_place_rounds_half_up_on_the_cent(self):
        """85.005 is 8501, not 8500.

        The multiplication happens before the rounding, so the rounding happens once.
        Rounding to two places first and then multiplying would round twice and can
        differ by a cent — which is the sort of difference that makes a total disagree
        with the sum of its lines.
        """
        assert money.to_cents("85.005") == 8501
        assert money.to_cents("85.004") == 8500

    def test_a_float_would_have_been_the_bug(self):
        """0.1 + 0.2 in floats is not 0.3. Through Decimal(str(...)) it is 30 cents."""
        assert money.to_cents(0.1) + money.to_cents(0.2) == money.to_cents("0.3")

    @pytest.mark.parametrize("nonsense", ["", "eighty-five", None, "85.00.00", "$85"])
    def test_anything_that_is_not_an_amount_is_refused(self, nonsense):
        """Refused rather than coerced to zero: a fee that silently became nothing is
        a session the ministry stops charging for and nobody notices."""
        with pytest.raises(ValueError):
            money.to_cents(nonsense)


class TestFromCents:
    def test_it_round_trips(self):
        for cents in (0, 1, 99, 100, 8550, 123456):
            assert money.to_cents(money.from_cents(cents)) == cents

    def test_it_gives_a_decimal_a_form_field_can_use(self):
        assert money.from_cents(8550) == Decimal("85.50")


class TestFormatting:
    def test_a_plain_amount(self):
        assert money.format_cents(8500) == "$85.00"

    def test_the_cents_are_always_two_digits(self):
        assert money.format_cents(8505) == "$85.05"
        assert money.format_cents(5) == "$0.05"

    def test_thousands_are_grouped(self):
        assert money.format_cents(123456789) == "$1,234,567.89"

    def test_a_negative_puts_the_sign_before_the_symbol(self):
        """-$12.50, not $-12.50. A credit on an invoice is read by the payer."""
        assert money.format_cents(-1250) == "-$12.50"

    def test_a_currency_without_a_symbol_falls_back_to_its_code(self):
        """Plain and correct beats guessing a symbol."""
        assert money.format_cents(8500, currency="chf") == "CHF 85.00"

    def test_the_ministrys_currency_is_used_when_none_is_given(self, settings):
        settings.BILLING_CURRENCY = "gbp"

        assert money.format_cents(8500) == "£85.00"


@pytest.mark.django_db
class TestInvoiceNumbering:
    def test_it_looks_like_a_reference_somebody_can_read_aloud(self, settings):
        settings.BILLING_INVOICE_PREFIX = "BC"

        number = next_invoice_number()

        assert number.startswith("BC-")
        assert len(number) == len("BC-000001")

    def test_every_number_is_different(self):
        numbers = [next_invoice_number() for _ in range(25)]

        assert len(set(numbers)) == 25

    def test_the_prefix_is_the_ministrys(self, settings):
        settings.BILLING_INVOICE_PREFIX = "GRACE"

        assert next_invoice_number().startswith("GRACE-")

    def test_a_rolled_back_transaction_leaves_a_gap_and_not_a_collision(self):
        """The trade this design makes, asserted so it reads as a decision.

        ``nextval`` is not transactional, so a number consumed by work that is undone
        is gone. That is the right way round: a gap is explainable to an accountant,
        two invoices with the same number are not.
        """
        with connection.cursor() as cursor:
            # S608: the interpolated name is a module constant, not input.
            cursor.execute(f"SELECT last_value FROM {SEQUENCE_NAME}")  # noqa: S608
            (before,) = cursor.fetchone()

        next_invoice_number()

        with connection.cursor() as cursor:
            # S608: the interpolated name is a module constant, not input.
            cursor.execute(f"SELECT last_value FROM {SEQUENCE_NAME}")  # noqa: S608
            (after,) = cursor.fetchone()

        assert after > before
