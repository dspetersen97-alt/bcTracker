"""
The fee schedule, session records, and the life of an invoice.

Everything here goes through ``apps.billing.services``, which is the only thing in
the application that writes an amount. Four properties carry the file, and they are
the four the module docstrings promise:

  * **A fee is snapshotted once.** Raising the ministry's rates must not reprice last
    month, and an invoice reissued next year has to say what it said at the time.
  * **An issued invoice is immutable.** A counselee told they owe $340 must not find a
    different number there tomorrow. Correcting one means voiding it and raising
    another, which is why voiding releases its sessions.
  * **Money is only ever totalled one way.** ``recalculate_payments`` sums the payment
    rows; nothing increments a counter. That is what makes a staff member and a
    webhook writing at the same instant safe.
  * **A missing fee is a gap somebody is told about**, not a zero that disappears into
    the totals. The path from "nobody set a rate" through the billing page to a
    correction is asserted end to end.
"""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.billing import services
from apps.billing.models import (
    Fee,
    FeeKind,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    SessionRecord,
)
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember
from apps.scheduling.models import Booking, BookingStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def ministry_rate(db):
    """$85 a session, in force for the last year."""
    return Fee.objects.create(
        kind=FeeKind.SESSION,
        amount_cents=8500,
        effective_from=org_today() - timedelta(days=365),
    )


@pytest.fixture
def case(counselor, counselee):
    case = Case.objects.create(counselor=counselor, label="Ashford — individual")
    CaseMember.objects.create(case=case, counselee=counselee)
    return case


@pytest.fixture
def biller(make_user):
    return make_user(Role.FINANCIAL_ADMIN)


@pytest.fixture
def session(case, counselee, ministry_rate, biller):
    return services.record_session(case=case, counselee=counselee, actor=biller)


@pytest.fixture
def draft(case, counselee, session, biller):
    return services.create_invoice(case=case, counselee=counselee, sessions=[session], actor=biller)


@pytest.fixture
def issued(draft, biller):
    return services.issue_invoice(draft, actor=biller)


# --- the fee schedule ------------------------------------------------------


class TestResolvingAFee:
    def test_the_ministrys_rate_applies_by_default(self, ministry_rate, counselor):
        assert Fee.objects.resolve(kind=FeeKind.SESSION, counselor=counselor) == ministry_rate

    def test_a_rate_set_for_one_counselor_beats_it(self, ministry_rate, counselor):
        """A supervisor charged differently, or a counselor in training charged nothing."""
        theirs = Fee.objects.create(
            kind=FeeKind.SESSION,
            counselor=counselor,
            amount_cents=12000,
            effective_from=org_today() - timedelta(days=10),
        )

        assert Fee.objects.resolve(kind=FeeKind.SESSION, counselor=counselor) == theirs

    def test_and_not_for_anybody_else(self, ministry_rate, counselor, other_counselor):
        Fee.objects.create(
            kind=FeeKind.SESSION,
            counselor=counselor,
            amount_cents=12000,
            effective_from=org_today() - timedelta(days=10),
        )

        assert Fee.objects.resolve(kind=FeeKind.SESSION, counselor=other_counselor) == ministry_rate

    def test_the_latest_rate_in_force_wins(self, ministry_rate, counselor):
        """How a rate change is made: a new row from the day it takes effect."""
        raised = Fee.objects.create(
            kind=FeeKind.SESSION,
            amount_cents=9500,
            effective_from=org_today() - timedelta(days=1),
        )

        assert Fee.objects.resolve(kind=FeeKind.SESSION, counselor=counselor) == raised

    def test_a_rate_that_starts_tomorrow_does_not_apply_today(self, ministry_rate, counselor):
        Fee.objects.create(
            kind=FeeKind.SESSION,
            amount_cents=9500,
            effective_from=org_today() + timedelta(days=1),
        )

        assert Fee.objects.resolve(kind=FeeKind.SESSION, counselor=counselor) == ministry_rate

    def test_a_withdrawn_rate_stops_applying(self, ministry_rate, counselor):
        ministry_rate.effective_to = org_today() - timedelta(days=1)
        ministry_rate.save(update_fields=["effective_to"])

        assert Fee.objects.resolve(kind=FeeKind.SESSION, counselor=counselor) is None

    def test_an_older_session_still_resolves_the_rate_of_its_day(self, ministry_rate, counselor):
        """The reason ``on`` is an argument: an invoice raised in April for a session
        in February is priced as February."""
        Fee.objects.create(
            kind=FeeKind.SESSION,
            amount_cents=9500,
            effective_from=org_today() - timedelta(days=5),
        )

        resolved = Fee.objects.resolve(
            kind=FeeKind.SESSION, counselor=counselor, on=org_today() - timedelta(days=30)
        )

        assert resolved == ministry_rate

    def test_nothing_configured_is_None_rather_than_zero(self, counselor):
        """Refused rather than invented. ``record_session`` turns this into a visible
        gap; a zero would turn it into a session nobody is ever charged for."""
        assert Fee.objects.resolve(kind=FeeKind.SESSION, counselor=counselor) is None

    def test_each_kind_is_resolved_separately(self, ministry_rate, counselor):
        """A ministry that charges for a session and not for a missed one."""
        assert Fee.objects.resolve(kind=FeeKind.NO_SHOW, counselor=counselor) is None


class TestRecordingASession:
    def test_the_fee_is_frozen_into_the_row(self, case, counselee, ministry_rate, biller):
        session = services.record_session(case=case, counselee=counselee, actor=biller)

        assert (session.fee_cents, session.is_billable) == (8500, True)
        assert session.fee == ministry_rate

    def test_raising_the_rate_afterwards_does_not_reprice_it(
        self, case, counselee, ministry_rate, biller
    ):
        """The property the whole snapshot design exists for."""
        session = services.record_session(case=case, counselee=counselee, actor=biller)

        Fee.objects.create(kind=FeeKind.SESSION, amount_cents=15000, effective_from=org_today())
        session.refresh_from_db()

        assert session.fee_cents == 8500

    def test_withdrawing_the_rate_afterwards_does_not_erase_it(
        self, case, counselee, ministry_rate, biller
    ):
        """``Fee.fee`` is SET_NULL, so the pointer can go; the amount cannot."""
        session = services.record_session(case=case, counselee=counselee, actor=biller)

        ministry_rate.delete()
        session.refresh_from_db()

        assert (session.fee_cents, session.fee_id) == (8500, None)

    def test_with_no_rate_it_is_recorded_and_says_why(self, case, counselee, biller):
        """Not refused, and not billed at nothing in silence. The session happened."""
        session = services.record_session(case=case, counselee=counselee, actor=biller)

        assert session.is_billable is False
        assert session.needs_pricing is True
        assert session.waived_reason

    def test_a_rate_of_zero_is_a_decision_and_not_a_gap(self, case, counselee, biller):
        """The difference that stops the billing page nagging for ever about
        something the ministry has already decided not to charge for."""
        Fee.objects.create(
            kind=FeeKind.SESSION, amount_cents=0, effective_from=org_today() - timedelta(days=1)
        )

        session = services.record_session(case=case, counselee=counselee, actor=biller)

        assert (session.is_billable, session.needs_pricing) == (False, False)

    def test_it_is_audited(self, case, counselee, ministry_rate, biller):
        services.record_session(case=case, counselee=counselee, actor=biller)

        assert AuditEvent.objects.filter(verb=AuditVerb.SESSION_RECORDED).exists()

    def test_the_line_it_would_put_on_an_invoice_says_nothing_about_the_counseling(self, session):
        description = session.line_description()

        assert "Counseling session" in description
        assert session.counselee.first_name not in description


class TestAmendingASession:
    def test_an_unpriced_session_can_be_given_a_fee(self, case, counselee, biller):
        session = services.record_session(case=case, counselee=counselee, actor=biller)

        services.amend_session(session, fee_cents=8500, is_billable=True, actor=biller)
        session.refresh_from_db()

        assert (session.fee_cents, session.is_billable, session.needs_pricing) == (
            8500,
            True,
            False,
        )

    def test_waiving_one_takes_it_off_the_list_of_work(self, case, counselee, biller):
        """The bug this field exists to prevent: a session the office deliberately
        waived reappearing for ever under "no fee set"."""
        session = services.record_session(case=case, counselee=counselee, actor=biller)

        services.amend_session(
            session,
            fee_cents=0,
            is_billable=False,
            waived_reason="Covered by the benevolence fund",
            actor=biller,
        )

        assert list(services.unpriced_sessions(actor=biller)) == []

    def test_waiving_one_without_saying_why_is_refused(self, session, biller):
        with pytest.raises(services.BillingError):
            services.amend_session(session, fee_cents=0, is_billable=False, actor=biller)

    def test_a_negative_fee_is_refused(self, session, biller):
        with pytest.raises(services.BillingError):
            services.amend_session(session, fee_cents=-100, is_billable=True, actor=biller)

    def test_an_invoiced_session_is_frozen(self, session, issued, biller):
        """The number is one the counselee has been told. Correcting it means voiding
        the invoice, which is what the error says."""
        with pytest.raises(services.AlreadyInvoiced):
            services.amend_session(session, fee_cents=100, is_billable=True, actor=biller)

    def test_and_thaws_again_when_the_invoice_is_withdrawn(self, session, issued, biller):
        services.void_invoice(issued, actor=biller, reason="Wrong person")

        services.amend_session(session, fee_cents=100, is_billable=True, actor=biller)
        session.refresh_from_db()

        assert session.fee_cents == 100


class TestASessionFromABooking:
    """The one place scheduling reaches into billing.

    A booking is an intention and can be moved, cancelled, or corrected a week later;
    a SessionRecord is the billable fact. These assertions are about the join between
    them holding under a counselor changing their mind.
    """

    @pytest.fixture
    def booking(self, case, counselor, counselee):
        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        return Booking.objects.create(
            counselor=counselor,
            case=case,
            counselee=counselee,
            slot=Booking.range_for(now - timedelta(days=2), 60),
            status=BookingStatus.CONFIRMED,
            created_by=counselor,
        )

    def test_marking_it_held_raises_a_billable_record(self, booking, counselor, ministry_rate):
        from apps.scheduling import services as scheduling

        scheduling.mark_completed(booking, actor=counselor)

        session = SessionRecord.objects.get(booking=booking)
        assert (session.kind, session.fee_cents) == (FeeKind.SESSION, 8500)

    def test_marking_it_held_twice_bills_it_once(self, booking, counselor, ministry_rate):
        """The OneToOne is the database's own guarantee; this asserts the service does
        not need to be saved by it."""
        from apps.scheduling import services as scheduling

        scheduling.mark_completed(booking, actor=counselor)
        services.session_for_booking(booking)

        assert SessionRecord.objects.filter(booking=booking).count() == 1

    def test_correcting_held_to_missed_reprices_it(self, booking, counselor, ministry_rate):
        from apps.scheduling import services as scheduling

        Fee.objects.create(
            kind=FeeKind.NO_SHOW,
            amount_cents=4000,
            effective_from=org_today() - timedelta(days=30),
        )
        scheduling.mark_completed(booking, actor=counselor)

        scheduling.mark_no_show(booking, actor=counselor)

        session = SessionRecord.objects.get(booking=booking)
        assert (session.kind, session.fee_cents) == (FeeKind.NO_SHOW, 4000)

    def test_but_not_once_it_is_on_an_invoice(self, booking, counselor, biller, ministry_rate):
        """A counselor correcting themselves after the bill went out cannot silently
        change the bill. The office voids it and raises another."""
        from apps.scheduling import services as scheduling

        scheduling.mark_completed(booking, actor=counselor)
        session = SessionRecord.objects.get(booking=booking)
        services.issue_invoice(
            services.create_invoice(
                case=session.case,
                counselee=session.counselee,
                sessions=[session],
                actor=biller,
            ),
            actor=biller,
        )

        scheduling.mark_no_show(booking, actor=counselor)

        session.refresh_from_db()
        assert (session.kind, session.fee_cents) == (FeeKind.SESSION, 8500)

    def test_a_cancellation_with_notice_bills_nothing(self, case, counselor, counselee):
        from apps.scheduling import services as scheduling

        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        booking = Booking.objects.create(
            counselor=counselor,
            case=case,
            counselee=counselee,
            slot=Booking.range_for(now + timedelta(days=30), 60),
            status=BookingStatus.CONFIRMED,
            created_by=counselee,
        )

        scheduling.cancel(booking, actor=counselee, reason="Away that week")

        assert SessionRecord.objects.filter(booking=booking).count() == 0

    def test_a_late_cancellation_bills_only_if_a_rate_is_set(
        self, case, counselor, counselee, ministry_rate
    ):
        """The fee schedule's decision, not scheduling's: with no late-cancellation
        rate the hour is recorded as not chargeable rather than charged at the session
        rate."""
        from apps.scheduling import services as scheduling

        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        booking = Booking.objects.create(
            counselor=counselor,
            case=case,
            counselee=counselee,
            slot=Booking.range_for(now + timedelta(hours=2), 60),
            status=BookingStatus.CONFIRMED,
            created_by=counselee,
        )

        scheduling.cancel(booking, actor=counselee, reason="Something came up")

        session = SessionRecord.objects.get(booking=booking)
        assert session.kind == FeeKind.LATE_CANCELLATION
        assert session.is_billable is False

    def test_a_billing_failure_does_not_stop_an_appointment_being_closed_out(
        self, booking, counselor, ministry_rate, monkeypatch
    ):
        """An appointment that cannot be marked as held is worse than one not yet
        billed, so the hook swallows. Asserted, because a bare ``except`` that nobody
        checks is indistinguishable from a bug."""
        from apps.scheduling import services as scheduling

        def explode(*args, **kwargs):
            raise RuntimeError("the billing app is having a bad day")

        monkeypatch.setattr(services, "session_for_booking", explode)

        scheduling.mark_completed(booking, actor=counselor)

        booking.refresh_from_db()
        assert booking.status == BookingStatus.COMPLETED


# --- the invoice ----------------------------------------------------------


class TestDraftingAnInvoice:
    def test_it_is_footed_from_its_lines(self, draft):
        assert draft.total_cents == 8500
        assert draft.lines.count() == 1

    def test_it_starts_as_a_draft_with_no_dates(self, draft):
        assert draft.status == InvoiceStatus.DRAFT
        assert (draft.issued_on, draft.due_on) == (None, None)

    def test_nobody_is_emailed(self, draft):
        assert mail.outbox == []

    def test_a_session_from_another_case_is_refused(
        self, case, counselee, biller, ministry_rate, other_counselor, make_user
    ):
        """Refused, not silently dropped: an invoice that quietly omits a session is
        how an hour goes unbilled for ever."""
        elsewhere = Case.objects.create(counselor=other_counselor, label="Someone else")
        stranger = make_user(Role.COUNSELEE)
        CaseMember.objects.create(case=elsewhere, counselee=stranger)
        theirs = services.record_session(case=elsewhere, counselee=stranger, actor=biller)

        with pytest.raises(services.BillingError):
            services.create_invoice(case=case, counselee=counselee, sessions=[theirs], actor=biller)

    def test_a_session_belonging_to_the_other_spouse_is_refused(
        self, case, counselee, biller, ministry_rate, make_user
    ):
        """One invoice is addressed to one person. On a couple's case each gets their
        own, which is the same rule as everywhere else in this application."""
        spouse = make_user(Role.COUNSELEE)
        CaseMember.objects.create(case=case, counselee=spouse)
        theirs = services.record_session(case=case, counselee=spouse, actor=biller)

        with pytest.raises(services.BillingError):
            services.create_invoice(case=case, counselee=counselee, sessions=[theirs], actor=biller)

    def test_an_unbillable_session_is_refused(self, case, counselee, biller):
        unpriced = services.record_session(case=case, counselee=counselee, actor=biller)

        with pytest.raises(services.BillingError):
            services.create_invoice(
                case=case, counselee=counselee, sessions=[unpriced], actor=biller
            )

    def test_a_session_already_on_a_live_invoice_is_refused(
        self, case, counselee, session, draft, biller
    ):
        with pytest.raises(services.AlreadyInvoiced):
            services.create_invoice(
                case=case, counselee=counselee, sessions=[session], actor=biller
            )

    def test_a_charge_that_did_not_come_from_a_session_can_be_added(self, draft, biller):
        services.add_line(
            draft, description="Workbook", unit_amount_cents=1250, quantity=2, actor=biller
        )
        draft.refresh_from_db()

        assert draft.total_cents == 8500 + 2500

    def test_a_line_can_be_taken_off_again(self, draft, biller):
        line = services.add_line(
            draft, description="Workbook", unit_amount_cents=1250, actor=biller
        )

        services.remove_line(line, actor=biller)
        draft.refresh_from_db()

        assert draft.total_cents == 8500


class TestIssuingAnInvoice:
    def test_it_gets_a_date_and_a_due_date(self, draft, biller, settings):
        settings.BILLING_DUE_DAYS = 30

        invoice = services.issue_invoice(draft, actor=biller)

        assert invoice.status == InvoiceStatus.OPEN
        assert invoice.issued_on == org_today()
        assert invoice.due_on == org_today() + timedelta(days=30)

    def test_the_payer_is_emailed_the_amount_and_the_reference(self, draft, biller):
        """Unlike a messaging notification, a bill says what it is. See notify.py."""
        invoice = services.issue_invoice(draft, actor=biller)

        assert len(mail.outbox) == 1
        body = mail.outbox[0].body
        assert invoice.number in body
        assert "$85.00" in body

    def test_and_nothing_about_the_counseling(self, draft, biller, case):
        invoice = services.issue_invoice(draft, actor=biller)

        body = mail.outbox[0].body
        assert case.label not in body
        assert case.counselor.full_name not in body
        assert "session" not in body.lower()
        assert invoice.lines.first().description not in body

    def test_a_deactivated_counselee_is_not_emailed_and_the_invoice_still_stands(
        self, draft, biller, counselee
    ):
        counselee.is_active = False
        counselee.save(update_fields=["is_active"])

        invoice = services.issue_invoice(draft, actor=biller)

        assert invoice.status == InvoiceStatus.OPEN
        assert mail.outbox == []

    def test_issuing_it_twice_is_refused(self, issued, biller):
        with pytest.raises(services.BillingError):
            services.issue_invoice(issued, actor=biller)

    def test_an_empty_invoice_cannot_go_out(self, case, counselee, biller):
        """A bill for $0.00 tells the recipient only that the office made a mistake."""
        empty = services.create_invoice(case=case, counselee=counselee, actor=biller)

        with pytest.raises(services.BillingError):
            services.issue_invoice(empty, actor=biller)

    def test_an_issued_invoice_cannot_have_a_line_added(self, issued, biller):
        with pytest.raises(services.NotEditable):
            services.add_line(issued, description="Extra", unit_amount_cents=100, actor=biller)

    def test_or_removed(self, issued, biller):
        with pytest.raises(services.NotEditable):
            services.remove_line(issued.lines.first(), actor=biller)


class TestVoidingAnInvoice:
    def test_it_keeps_the_row_and_records_why(self, issued, biller):
        services.void_invoice(issued, actor=biller, reason="Billed the wrong spouse")
        issued.refresh_from_db()

        assert issued.status == InvoiceStatus.VOID
        assert issued.void_reason == "Billed the wrong spouse"
        assert issued.voided_at is not None

    def test_a_reason_is_required(self, issued, biller):
        with pytest.raises(services.BillingError):
            services.void_invoice(issued, actor=biller, reason="   ")

    def test_its_sessions_become_available_again(self, issued, session, biller):
        """What makes "void it and raise another" the correction path."""
        services.void_invoice(issued, actor=biller, reason="Wrong amount")

        assert session in SessionRecord.objects.uninvoiced()

    def test_and_a_corrected_invoice_can_be_raised_from_them(
        self, issued, session, case, counselee, biller
    ):
        services.void_invoice(issued, actor=biller, reason="Wrong amount")

        replacement = services.create_invoice(
            case=case, counselee=counselee, sessions=[session], actor=biller
        )

        assert replacement.total_cents == 8500
        assert replacement.number != issued.number

    def test_an_invoice_with_money_against_it_cannot_be_voided(self, issued, biller):
        """That would leave the ministry holding money against nothing. It is refunded."""
        services.record_payment(issued, amount_cents=1000, method=PaymentMethod.CASH, actor=biller)

        with pytest.raises(services.BillingError):
            services.void_invoice(issued, actor=biller, reason="Changed our minds")

    def test_voiding_a_void_invoice_is_a_no_op_rather_than_an_error(self, issued, biller):
        services.void_invoice(issued, actor=biller, reason="Wrong amount")

        services.void_invoice(issued, actor=biller, reason="Again")

        issued.refresh_from_db()
        assert issued.void_reason == "Wrong amount"


class TestWritingAnInvoiceOff:
    def test_it_stays_payable(self, issued, biller):
        """Voiding says the invoice was wrong; writing off says it was right. Somebody
        settling up a year later must not be refused."""
        services.write_off_invoice(issued, actor=biller, reason="No longer contactable")
        issued.refresh_from_db()

        assert issued.status == InvoiceStatus.UNCOLLECTIBLE
        assert issued.is_payable is True

    def test_and_paying_it_marks_it_paid(self, issued, biller):
        services.write_off_invoice(issued, actor=biller, reason="No longer contactable")

        services.record_payment(issued, amount_cents=8500, method=PaymentMethod.CHECK, actor=biller)
        issued.refresh_from_db()

        assert issued.status == InvoiceStatus.PAID

    def test_nobody_is_emailed(self, issued, biller):
        mail.outbox.clear()

        services.write_off_invoice(issued, actor=biller, reason="No longer contactable")

        assert mail.outbox == []

    def test_a_draft_cannot_be_written_off(self, draft, biller):
        with pytest.raises(services.BillingError):
            services.write_off_invoice(draft, actor=biller, reason="Never mind")


# --- money in -------------------------------------------------------------


class TestRecordingAPayment:
    def test_it_reduces_the_balance(self, issued, biller):
        services.record_payment(issued, amount_cents=2500, method=PaymentMethod.CASH, actor=biller)
        issued.refresh_from_db()

        assert (issued.amount_paid_cents, issued.balance_cents) == (2500, 6000)
        assert issued.status == InvoiceStatus.OPEN

    def test_paying_it_in_full_marks_it_paid(self, issued, biller):
        services.record_payment(issued, amount_cents=8500, method=PaymentMethod.CHECK, actor=biller)
        issued.refresh_from_db()

        assert issued.status == InvoiceStatus.PAID

    def test_two_part_payments_add_up(self, issued, biller):
        services.record_payment(issued, amount_cents=4000, method=PaymentMethod.CASH, actor=biller)
        services.record_payment(issued, amount_cents=4500, method=PaymentMethod.CASH, actor=biller)
        issued.refresh_from_db()

        assert issued.status == InvoiceStatus.PAID
        assert issued.amount_paid_cents == 8500

    def test_the_total_is_the_sum_of_the_rows_and_never_a_running_counter(self, issued, biller):
        """Asserted by contradiction: a row written behind the service's back and then
        a re-foot. If anything incremented a counter this would double-count."""
        services.record_payment(issued, amount_cents=4000, method=PaymentMethod.CASH, actor=biller)
        Payment.objects.create(invoice=issued, amount_cents=1000, method=PaymentMethod.CASH)

        services.recalculate_payments(issued)

        assert issued.amount_paid_cents == 5000

    def test_an_overpayment_leaves_a_visible_credit(self, issued, biller):
        """Not clamped at zero: somebody rounding up is common, and a credit the
        office can see beats a difference that disappears."""
        services.record_payment(issued, amount_cents=9000, method=PaymentMethod.CASH, actor=biller)
        issued.refresh_from_db()

        assert issued.balance_cents == -500
        assert issued.display_balance() == "-$5.00"

    def test_a_payment_of_nothing_is_refused(self, issued, biller):
        with pytest.raises(services.BillingError):
            services.record_payment(issued, amount_cents=0, method=PaymentMethod.CASH, actor=biller)

    def test_a_draft_cannot_take_a_payment(self, draft, biller):
        """Money against a bill nobody has been sent is a mistake worth refusing."""
        with pytest.raises(services.BillingError):
            services.record_payment(
                draft, amount_cents=8500, method=PaymentMethod.CASH, actor=biller
            )

    def test_a_void_invoice_cannot_take_one_either(self, issued, biller):
        services.void_invoice(issued, actor=biller, reason="Wrong person")

        with pytest.raises(services.BillingError):
            services.record_payment(
                issued, amount_cents=8500, method=PaymentMethod.CASH, actor=biller
            )

    def test_no_receipt_is_emailed_for_a_payment_taken_in_person(self, issued, biller):
        """The payer was in the room. Only a card payment gets a receipt, because they
        were not — see notify.payment_received."""
        mail.outbox.clear()

        services.record_payment(issued, amount_cents=8500, method=PaymentMethod.CASH, actor=biller)

        assert mail.outbox == []

    def test_it_is_audited_with_the_amount(self, issued, biller):
        services.record_payment(issued, amount_cents=8500, method=PaymentMethod.CHECK, actor=biller)

        event = AuditEvent.objects.filter(verb=AuditVerb.PAYMENT_RECORDED).first()
        assert event.metadata["amount_cents"] == 8500


class TestReversingAPayment:
    @pytest.fixture
    def payment(self, issued, biller):
        return services.record_payment(
            issued,
            amount_cents=8500,
            method=PaymentMethod.CHECK,
            reference="check 1041",
            actor=biller,
        )

    def test_it_writes_the_opposite_row_rather_than_editing(self, payment, issued, biller):
        reversal = services.reverse_payment(payment, actor=biller, reason="Check bounced")

        payment.refresh_from_db()
        assert payment.amount_cents == 8500
        assert reversal.amount_cents == -8500
        assert reversal.reverses_id == payment.pk

    def test_the_invoice_goes_back_to_awaiting_payment(self, payment, issued, biller):
        services.reverse_payment(payment, actor=biller, reason="Check bounced")
        issued.refresh_from_db()

        assert issued.status == InvoiceStatus.OPEN
        assert issued.balance_cents == 8500

    def test_a_payment_cannot_be_reversed_twice(self, payment, biller):
        services.reverse_payment(payment, actor=biller, reason="Check bounced")

        with pytest.raises(services.BillingError):
            services.reverse_payment(payment, actor=biller, reason="Again")

    def test_a_reversal_cannot_itself_be_reversed(self, payment, biller):
        reversal = services.reverse_payment(payment, actor=biller, reason="Check bounced")

        with pytest.raises(services.BillingError):
            services.reverse_payment(reversal, actor=biller, reason="Undo the undo")

    def test_both_entries_stay_on_the_record(self, payment, issued, biller):
        services.reverse_payment(payment, actor=biller, reason="Check bounced")

        assert issued.payments.count() == 2


class TestAStripePayment:
    def test_it_is_recorded_as_a_card_payment_with_no_actor(self, issued):
        """Nobody in this ministry did it; Stripe did. The null actor says so."""
        payment = services.apply_stripe_payment(
            issued, payment_intent_id="pi_123", amount_cents=8500
        )

        assert (payment.method, payment.recorded_by_id) == (PaymentMethod.CARD, None)

    def test_the_same_payment_intent_is_only_recorded_once(self, issued):
        """The failure that matters most, guarded three ways. This is the explicit one."""
        first = services.apply_stripe_payment(issued, payment_intent_id="pi_123", amount_cents=8500)
        second = services.apply_stripe_payment(
            issued, payment_intent_id="pi_123", amount_cents=8500
        )

        assert first.pk == second.pk
        assert issued.payments.count() == 1

    def test_and_the_database_refuses_it_too(self, issued):
        """The service's check removed, to prove the index behind it is real."""
        from django.db import IntegrityError, transaction

        services.apply_stripe_payment(issued, payment_intent_id="pi_123", amount_cents=8500)

        with pytest.raises(IntegrityError), transaction.atomic():
            Payment.objects.create(
                invoice=issued,
                amount_cents=8500,
                method=PaymentMethod.CARD,
                stripe_payment_intent_id="pi_123",
            )


# --- what the billing page needs -----------------------------------------


class TestTheBillingPagesLists:
    def test_uninvoiced_work_is_grouped_by_who_owes(
        self, case, counselee, biller, ministry_rate, make_user
    ):
        """One invoice is addressed to one person, so the page cannot offer "invoice
        this case" and pick a spouse."""
        spouse = make_user(Role.COUNSELEE)
        CaseMember.objects.create(case=case, counselee=spouse)
        services.record_session(case=case, counselee=counselee, actor=biller)
        services.record_session(case=case, counselee=counselee, actor=biller)
        services.record_session(case=case, counselee=spouse, actor=biller)

        groups = services.uninvoiced_work(actor=biller)

        assert len(groups) == 2
        assert {group["total_cents"] for group in groups} == {17000, 8500}

    def test_an_invoiced_session_drops_off_the_list(self, session, draft, biller):
        assert services.uninvoiced_work(actor=biller) == []

    def test_a_voided_invoices_sessions_come_back(self, session, issued, biller):
        services.void_invoice(issued, actor=biller, reason="Wrong amount")

        assert len(services.uninvoiced_work(actor=biller)) == 1

    def test_unpriced_sessions_are_listed_separately(self, case, counselee, biller):
        services.record_session(case=case, counselee=counselee, actor=biller)

        assert services.unpriced_sessions(actor=biller).count() == 1

    def test_another_counselors_work_is_not_in_it(self, session, other_counselor):
        """Scoped like everything else, even though the page is not a counselor's to
        open: what these functions return is never "everything"."""
        assert services.uninvoiced_work(actor=other_counselor) == []
        assert services.unpriced_sessions(actor=other_counselor).count() == 0


class TestOverdueAndReminders:
    @pytest.fixture
    def overdue(self, issued):
        """Backdated in the database rather than by faking a clock.

        Both dates move, because ``invoice_is_not_due_before_it_is_issued`` is a
        check constraint and Postgres applies it to an UPDATE as well as an INSERT —
        an invoice issued today and due yesterday is not a state this table permits.
        """
        Invoice.objects.filter(pk=issued.pk).update(
            issued_on=org_today() - timedelta(days=40),
            due_on=org_today() - timedelta(days=1),
        )
        issued.refresh_from_db()
        return issued

    def test_an_invoice_with_time_left_is_not_overdue(self, issued):
        assert issued.is_overdue is False
        assert services.due_invoice_reminders().count() == 0

    def test_an_overdue_one_is(self, overdue):
        assert overdue.is_overdue is True
        assert list(services.due_invoice_reminders()) == [overdue]

    def test_a_reminder_is_sent_once_and_only_once(self, overdue):
        mail.outbox.clear()

        assert services.send_invoice_reminder(overdue) is True
        assert services.send_invoice_reminder(overdue) is False
        assert len(mail.outbox) == 1

    def test_and_the_invoice_leaves_the_queue(self, overdue):
        services.send_invoice_reminder(overdue)

        assert services.due_invoice_reminders().count() == 0

    def test_a_paid_invoice_is_never_chased(self, overdue, biller):
        services.record_payment(
            overdue, amount_cents=8500, method=PaymentMethod.CHECK, actor=biller
        )

        assert services.due_invoice_reminders().count() == 0

    def test_a_void_one_is_not_chased_either(self, overdue, biller):
        services.void_invoice(overdue, actor=biller, reason="Wrong person")

        assert services.due_invoice_reminders().count() == 0
