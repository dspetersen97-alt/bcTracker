"""
Cents, formatting, and invoice numbering.

Small, fast, and worth having its own file, because these are the functions every
amount in the application passes through. A bug in ``to_cents`` is a bug in every
invoice ever raised, and it would not show up as an error — it would show up as a
ministry that quietly charged the wrong number.

The numbering tests are here rather than with the invoice lifecycle because what
they assert is a property of the reference itself and not of an invoice: that it is
short enough to read aloud, that it carries the ministry's prefix, and that it is the
same ten digits as the invoice's URL. See apps/billing/numbering.py.
"""

from decimal import Decimal

import pytest

from apps.billing import money
from apps.billing.models import Invoice
from apps.billing.numbering import invoice_number_for, invoice_prefix
from apps.core.ids import generate_public_id
from apps.counseling.models import Case


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


class TestInvoiceNumbering:
    """The printed reference. See apps/billing/numbering.py for why it is the
    invoice's public id rather than a counter of its own."""

    def test_it_looks_like_a_reference_somebody_can_read_aloud(self, settings):
        settings.BILLING_INVOICE_PREFIX = "BC"

        number = invoice_number_for("4820193756")

        assert number == "BC-4820193756"

    def test_the_prefix_is_the_ministrys(self, settings):
        settings.BILLING_INVOICE_PREFIX = "GRACE"

        assert invoice_number_for(generate_public_id()).startswith("GRACE-")

    def test_a_ministry_that_set_no_prefix_still_gets_one(self, settings):
        """Empty rather than absent is the state a half-filled .env leaves, and an
        invoice numbered "-4820193756" would look like a bug to the person paying."""
        settings.BILLING_INVOICE_PREFIX = ""

        assert invoice_prefix() == "BC"

    def test_every_number_is_different(self):
        """A property of the id, not of a sequence: nothing is read before a value is
        chosen, so there is no read-then-write race for two staff to lose."""
        numbers = [invoice_number_for(generate_public_id()) for _ in range(25)]

        assert len(set(numbers)) == 25

    @pytest.mark.django_db
    def test_an_invoice_is_numbered_with_its_own_public_id(self, counselor, counselee):
        """The point of the whole arrangement: the reference on the bill and the id in
        the URL are the same digits, so a payer quoting one can be found by the other."""
        case = Case.objects.create(counselor=counselor, label="Ashford — individual")

        invoice = Invoice.objects.create(case=case, counselee=counselee)

        assert invoice.number == f"{invoice_prefix()}-{invoice.public_id}"

    @pytest.mark.django_db
    def test_a_reissued_invoice_keeps_the_reference_it_went_out_with(self, counselor, counselee):
        """Saving an invoice again must not renumber it. A payment quoting the old
        reference would otherwise match nothing."""
        case = Case.objects.create(counselor=counselor, label="Ashford — individual")
        invoice = Invoice.objects.create(case=case, counselee=counselee)
        was = invoice.number

        invoice.memo = "Sessions for March"
        invoice.save()
        invoice.refresh_from_db()

        assert invoice.number == was
