"""
The two billing jobs the cron sidecar runs.

Both are written to be safe to run more often than they are scheduled, and that is
what most of this file asserts — because the way a cron job fails is not usually a
crash, it is running twice.

``send_invoice_reminders`` sends **one reminder per invoice, ever**. Not a first
notice and a second and a final demand: this is a counseling ministry, and somebody
who has not paid is either busy or cannot afford it. The second case is a pastoral
conversation, so the computer says one thing once and then leaves it to the office.

``reconcile_stripe`` asks the question the webhook cannot: for every invoice that
still shows a balance and once sent somebody to Stripe, does Stripe think it was
paid? A missed delivery ends with a counselee who has paid being reminded that they
owe money, and being quite right to be annoyed. Stripe is stubbed at
``apps.billing.stripe.client`` rather than over HTTP — the client's own error
handling is not what this command is for.
"""

import io
from datetime import timedelta

import pytest
from django.core import mail
from django.core.management import call_command

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
    StripeEvent,
    StripeEventOutcome,
)
from apps.billing.stripe import client as stripe_client
from apps.billing.stripe.errors import StripeUnavailable
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember

pytestmark = pytest.mark.django_db


@pytest.fixture
def biller(make_user):
    return make_user(Role.FINANCIAL_ADMIN)


@pytest.fixture
def bill(counselor, biller, make_user):
    """A factory for one issued invoice, dated as far in the past as asked.

    ``issued_on`` and ``due_on`` are moved with an UPDATE rather than by faking the
    clock: they are business dates, and ``invoice_is_not_due_before_it_is_issued`` is
    a check constraint Postgres applies to an UPDATE as well as an INSERT, so both
    have to move together.
    """
    Fee.objects.create(
        kind=FeeKind.SESSION,
        amount_cents=8500,
        effective_from=org_today() - timedelta(days=400),
    )

    def _bill(*, overdue_by=None, counselee=None):
        counselee = counselee or make_user(Role.COUNSELEE)
        case = Case.objects.create(counselor=counselor, label=f"Case for {counselee.pk}")
        CaseMember.objects.create(case=case, counselee=counselee)
        session = services.record_session(case=case, counselee=counselee, actor=biller)
        invoice = services.issue_invoice(
            services.create_invoice(
                case=case, counselee=counselee, sessions=[session], actor=biller
            ),
            actor=biller,
        )
        if overdue_by is not None:
            Invoice.objects.filter(pk=invoice.pk).update(
                issued_on=org_today() - timedelta(days=overdue_by + 30),
                due_on=org_today() - timedelta(days=overdue_by),
            )
            invoice.refresh_from_db()
        mail.outbox.clear()
        return invoice

    return _bill


def run(command, **options):
    """Run a command, returning what it wrote to each stream."""
    out, err = io.StringIO(), io.StringIO()
    call_command(command, stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


# --- reminders -------------------------------------------------------------


class TestSendInvoiceReminders:
    def test_an_overdue_invoice_is_reminded_about(self, bill):
        invoice = bill(overdue_by=3)

        out, _err = run("send_invoice_reminders")

        invoice.refresh_from_db()
        assert len(mail.outbox) == 1
        assert invoice.number in mail.outbox[0].subject
        assert invoice.reminder_sent_at is not None
        assert "1 reminder(s) sent" in out

    def test_an_invoice_with_time_left_is_left_alone(self, bill):
        bill()

        run("send_invoice_reminders")

        assert mail.outbox == []

    def test_running_it_again_sends_nothing(self, bill):
        """The property that makes it safe on a cron line. Once per invoice, ever."""
        bill(overdue_by=3)

        run("send_invoice_reminders")
        run("send_invoice_reminders")

        assert len(mail.outbox) == 1

    def test_two_overlapping_runs_cannot_double_send(self, bill):
        """The same claim as scheduling's reminder and LoginToken.consume: the
        conditional UPDATE happens before the mail, so the second caller finds nothing
        to claim rather than both passing the same filter."""
        invoice = bill(overdue_by=3)

        assert services.send_invoice_reminder(invoice) is True
        assert services.send_invoice_reminder(invoice) is False
        assert len(mail.outbox) == 1

    def test_a_dry_run_sends_nothing_and_marks_nothing(self, bill):
        invoice = bill(overdue_by=3)

        out, _err = run("send_invoice_reminders", dry_run=True)

        invoice.refresh_from_db()
        assert mail.outbox == []
        assert invoice.reminder_sent_at is None
        assert invoice.number in out
        assert "would be sent" in out

    def test_and_a_dry_run_does_not_stop_the_real_one_later(self, bill):
        bill(overdue_by=3)

        run("send_invoice_reminders", dry_run=True)
        run("send_invoice_reminders")

        assert len(mail.outbox) == 1

    def test_a_paid_invoice_is_never_chased(self, bill, biller):
        invoice = bill(overdue_by=3)
        services.record_payment(
            invoice, amount_cents=8500, method=PaymentMethod.CHECK, actor=biller
        )
        mail.outbox.clear()

        run("send_invoice_reminders")

        assert mail.outbox == []

    def test_a_part_paid_one_is_chased_for_the_balance(self, bill, biller):
        invoice = bill(overdue_by=3)
        services.record_payment(
            invoice, amount_cents=2000, method=PaymentMethod.CHECK, actor=biller
        )
        mail.outbox.clear()

        run("send_invoice_reminders")

        assert len(mail.outbox) == 1
        assert "$65.00" in mail.outbox[0].body

    def test_a_withdrawn_invoice_is_not_chased(self, bill, biller):
        invoice = bill(overdue_by=3)
        services.void_invoice(invoice, actor=biller, reason="Billed the wrong person")
        mail.outbox.clear()

        run("send_invoice_reminders")

        assert mail.outbox == []

    def test_one_written_off_is_not_chased_either(self, bill, biller):
        """It can still be paid — that is what keeps it in PAYABLE_STATUSES — but the
        ministry has stopped asking, which is what writing it off meant."""
        invoice = bill(overdue_by=3)
        services.write_off_invoice(invoice, actor=biller, reason="No longer contactable")
        mail.outbox.clear()

        run("send_invoice_reminders")

        assert mail.outbox == []

    def test_a_deactivated_counselee_is_marked_rather_than_retried_for_ever(self, bill):
        """No mail is sent, and the invoice is still marked. Leaving it unclaimed would
        put it back on the queue every night for the rest of time."""
        invoice = bill(overdue_by=3)
        invoice.counselee.is_active = False
        invoice.counselee.save(update_fields=["is_active"])

        run("send_invoice_reminders")

        invoice.refresh_from_db()
        assert mail.outbox == []
        assert invoice.reminder_sent_at is not None

    def test_each_invoice_is_reminded_about_separately(self, bill):
        """Two bills for the same person are two reminders, because they are two
        references and a single email would have to omit one of them."""
        first = bill(overdue_by=3)
        second = bill(overdue_by=5, counselee=first.counselee)

        out, _err = run("send_invoice_reminders")

        assert len(mail.outbox) == 2
        subjects = " ".join(message.subject for message in mail.outbox)
        assert first.number in subjects
        assert second.number in subjects
        assert "2 reminder(s) sent" in out

    def test_the_send_is_audited_with_no_actor(self, bill):
        """Nobody did this; a cron line did. The null actor says so."""
        bill(overdue_by=3)

        run("send_invoice_reminders")

        event = AuditEvent.objects.filter(verb=AuditVerb.INVOICE_REMINDER_SENT).first()
        assert event is not None
        assert event.actor is None

    def test_it_says_nothing_when_there_is_nothing_to_do(self, bill):
        out, err = run("send_invoice_reminders")

        assert "0 reminder(s) sent" in out
        assert err == ""


# --- reconciliation --------------------------------------------------------


@pytest.fixture
def stripe_on(settings):
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = "sk_test_not_real"  # noqa: S105
    return settings


@pytest.fixture
def stripe_says(monkeypatch):
    """Stub ``retrieve_checkout_session`` with a dict per session id.

    Stubbed at the client rather than over HTTP because what is under test is the
    command's reasoning about the answer, not the parsing of it — and because nothing
    in this suite is allowed to reach the network.
    """

    def _stub(sessions):
        def retrieve(session_id):
            if session_id not in sessions:
                raise StripeUnavailable(f"no such session {session_id}")
            return sessions[session_id]

        monkeypatch.setattr(stripe_client, "retrieve_checkout_session", retrieve)

    return _stub


@pytest.fixture
def awaiting_stripe(bill):
    """An overdue invoice that once sent its payer to a Stripe Checkout page."""

    def _make(session_id="cs_test_1", **kwargs):
        invoice = bill(**kwargs)
        invoice.stripe_checkout_session_id = session_id
        invoice.save(update_fields=["stripe_checkout_session_id"])
        return invoice

    return _make


def paid_session(session_id="cs_test_1", *, amount=8500, intent="pi_test_1"):
    return {
        "id": session_id,
        "payment_status": "paid",
        "amount_total": amount,
        "payment_intent": intent,
    }


class TestReconcileStripe:
    def test_it_exits_quietly_when_stripe_is_not_configured(self, awaiting_stripe, monkeypatch):
        """Not an error. A ministry taking cash and checks runs this way on purpose,
        and the cron line is the same either way — so the command must not fail, and
        must not call Stripe."""

        def explode(session_id):
            raise AssertionError("Stripe must not be called when it is not configured")

        monkeypatch.setattr(stripe_client, "retrieve_checkout_session", explode)
        awaiting_stripe()

        out, err = run("reconcile_stripe")

        assert "not configured" in out
        assert err == ""

    def test_a_payment_the_webhook_missed_is_recorded(
        self, stripe_on, stripe_says, awaiting_stripe
    ):
        invoice = awaiting_stripe(overdue_by=2)
        stripe_says({"cs_test_1": paid_session()})

        out, _err = run("reconcile_stripe")

        invoice.refresh_from_db()
        payment = invoice.payments.get()
        assert invoice.status == InvoiceStatus.PAID
        assert payment.stripe_payment_intent_id == "pi_test_1"
        assert payment.recorded_by_id is None
        assert "reconciled" in payment.reference
        assert "1 payment(s) recovered" in out

    def test_it_never_emails_anybody(self, stripe_on, stripe_says, awaiting_stripe):
        """A counselee who paid a week ago does not need a message at 3am, receipt or
        not."""
        awaiting_stripe(overdue_by=2)
        stripe_says({"cs_test_1": paid_session()})

        run("reconcile_stripe")

        assert mail.outbox == []

    def test_a_dry_run_reports_and_changes_nothing(self, stripe_on, stripe_says, awaiting_stripe):
        invoice = awaiting_stripe(overdue_by=2)
        stripe_says({"cs_test_1": paid_session()})

        out, _err = run("reconcile_stripe", dry_run=True)

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.OPEN
        assert Payment.objects.count() == 0
        assert invoice.number in out

    def test_an_unpaid_session_is_left_alone(self, stripe_on, stripe_says, awaiting_stripe):
        invoice = awaiting_stripe(overdue_by=2)
        stripe_says({"cs_test_1": {"id": "cs_test_1", "payment_status": "unpaid"}})

        run("reconcile_stripe")

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.OPEN
        assert Payment.objects.count() == 0

    def test_a_payment_the_webhook_already_recorded_is_not_posted_twice(
        self, stripe_on, stripe_says, awaiting_stripe
    ):
        """The case that will happen most often: the webhook worked. Running this
        alongside it must be a no-op, and must not claim to have recovered anything.
        """
        invoice = awaiting_stripe(overdue_by=2)
        services.apply_stripe_payment(invoice, payment_intent_id="pi_test_1", amount_cents=8500)
        stripe_says({"cs_test_1": paid_session()})

        out, _err = run("reconcile_stripe")

        invoice.refresh_from_db()
        assert invoice.payments.count() == 1
        assert "0 payment(s) recovered" in out

    def test_an_invoice_that_never_started_a_checkout_is_not_asked_about(
        self, stripe_on, stripe_says, bill
    ):
        bill(overdue_by=2)
        stripe_says({})  # any call would raise

        out, _err = run("reconcile_stripe")

        assert "0 invoice(s) checked" in out

    def test_an_invoice_older_than_the_window_is_skipped(
        self, stripe_on, stripe_says, awaiting_stripe
    ):
        """Stripe expires a Checkout Session anyway, so an ancient one is not going to
        be completed now."""
        awaiting_stripe(overdue_by=200)
        stripe_says({"cs_test_1": paid_session()})

        out, _err = run("reconcile_stripe", days=90)

        assert "0 invoice(s) checked" in out
        assert Payment.objects.count() == 0

    def test_a_paid_invoice_is_not_asked_about(
        self, stripe_on, stripe_says, awaiting_stripe, biller
    ):
        invoice = awaiting_stripe(overdue_by=2)
        services.record_payment(
            invoice, amount_cents=8500, method=PaymentMethod.CHECK, actor=biller
        )
        stripe_says({})

        out, _err = run("reconcile_stripe")

        assert "0 invoice(s) checked" in out

    def test_a_stripe_failure_is_reported_and_does_not_stop_the_run(
        self, stripe_on, stripe_says, awaiting_stripe
    ):
        unreadable = awaiting_stripe(session_id="cs_missing", overdue_by=2)
        readable = awaiting_stripe(session_id="cs_test_2", overdue_by=2)
        stripe_says({"cs_test_2": paid_session("cs_test_2", intent="pi_test_2")})

        out, err = run("reconcile_stripe")

        readable.refresh_from_db()
        unreadable.refresh_from_db()
        assert readable.status == InvoiceStatus.PAID
        assert unreadable.status == InvoiceStatus.OPEN
        assert unreadable.number in err
        assert "needing attention" in err

    def test_a_failed_webhook_delivery_is_reported_for_a_person(self, stripe_on, stripe_says):
        """The commonest cause is a paid Checkout Session naming an invoice this
        database does not hold — money in Stripe's account the ministry's books know
        nothing about. Not something a command can fix."""
        StripeEvent.objects.create(
            stripe_event_id="evt_bad",
            event_type="checkout.session.completed",
            outcome=StripeEventOutcome.FAILED,
            detail="Paid, but no matching invoice.",
        )
        stripe_says({})

        _out, err = run("reconcile_stripe")

        assert "evt_bad" in err
        assert "no matching invoice" in err

    def test_an_overpaid_invoice_is_reported_as_a_refund_owed(
        self, stripe_on, stripe_says, awaiting_stripe, biller
    ):
        """Usually a card payment landing the same day as a check for the same bill.
        Harmless, and somebody owes a refund."""
        invoice = awaiting_stripe(overdue_by=2)
        services.record_payment(
            invoice, amount_cents=9000, method=PaymentMethod.CHECK, actor=biller
        )
        stripe_says({})

        _out, err = run("reconcile_stripe")

        assert invoice.number in err
        assert "refund is owed" in err

    def test_a_clean_run_says_so_and_writes_nothing_to_stderr(
        self, stripe_on, stripe_says, awaiting_stripe
    ):
        awaiting_stripe(overdue_by=2)
        stripe_says({"cs_test_1": {"id": "cs_test_1", "payment_status": "unpaid"}})

        out, err = run("reconcile_stripe")

        assert "1 invoice(s) checked, 0 payment(s) recovered" in out
        assert err == ""

    def test_the_run_is_audited_with_its_counts(self, stripe_on, stripe_says, awaiting_stripe):
        awaiting_stripe(overdue_by=2)
        stripe_says({"cs_test_1": paid_session()})

        run("reconcile_stripe")

        event = AuditEvent.objects.filter(verb=AuditVerb.STRIPE_RECONCILED).first()
        assert event.actor is None
        assert event.metadata["checked"] == 1
        assert event.metadata["recovered"] == 1
        assert event.metadata["dry_run"] is False
