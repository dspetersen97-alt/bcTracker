"""
Everything that changes a session record, an invoice, or a payment.

Kept out of the views so the awkward rules are testable without HTTP, and so the
Stripe webhook and the reconciliation command act through exactly the same
functions a staff member's click does. Four rules run through all of it.

**A fee is snapshotted once and never recomputed.** ``record_session`` resolves the
schedule at the moment the session is recorded and writes the number into the row.
Nothing else in this module looks a fee up again. A ministry that raises its rates
in March has not repriced February, and reprinting an old invoice produces the old
invoice.

**An issued invoice is immutable.** Adding, removing, or repricing a line is only
possible while the invoice is a draft. Once it has been sent, the amount a counselee
was told is the amount, and the only ways to change the situation are a payment, a
void, or a write-off. This is enforced here as well as in ``rules.py``, because a
management command has no permission check.

**Money is only ever totalled one way.** ``recalculate_payments`` sums the payment
rows; nothing increments ``amount_paid_cents``. A read-modify-write on a counter is
how a webhook arriving while the office is typing a check into the same invoice ends
up recording one payment twice, or losing one entirely. The row is locked, the sum
is recomputed from the rows that exist, and the status follows from the result.

**Recording a payment twice is the failure that matters most.** Three independent
guards, and they are independent on purpose: ``StripeEvent`` refuses a redelivered
webhook, the partial unique index on ``Payment.stripe_payment_intent_id`` refuses a
second row for the same charge, and ``apply_stripe_payment`` checks for an existing
row before writing. Any one of them alone would be enough on a good day.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.billing import notify
from apps.billing.models import (
    PAYABLE_STATUSES,
    Fee,
    FeeKind,
    Invoice,
    InvoiceLineItem,
    InvoiceStatus,
    Payment,
    PaymentMethod,
    SessionRecord,
    paid_total_cents,
)
from apps.billing.numbering import next_invoice_number
from apps.core.dates import org_today

logger = logging.getLogger(__name__)


class BillingError(Exception):
    """Base for refusals whose message is safe to show the person acting."""


class NotEditable(BillingError):
    """The invoice has been issued, so its lines are fixed."""

    def __init__(self, message=None):
        super().__init__(message or _("This invoice has been sent and cannot be changed."))


class AlreadyInvoiced(BillingError):
    """A session is already on an invoice that stands."""


# --- recording what happened ----------------------------------------------

#: How a closed-out booking maps onto something chargeable. A booking in any other
#: state raises nothing: an appointment that is still in the diary has not happened,
#: and one cancelled with proper notice is what the notice period is *for*.
KIND_FOR_BOOKING = {
    "completed": FeeKind.SESSION,
    "no_show": FeeKind.NO_SHOW,
}


def kind_for_booking(booking):
    """What this booking is chargeable as, or None.

    A cancellation is only chargeable when it came too late to refill the hour, and
    that judgement was made and stored at the time — see
    ``scheduling.services.cancel`` and the note on ``was_late_cancellation``. It is
    read here rather than recomputed, because the notice period is a setting and the
    rule that applied is the one that was in force on the day.
    """
    if booking.status == "cancelled":
        return FeeKind.LATE_CANCELLATION if booking.was_late_cancellation else None
    return KIND_FOR_BOOKING.get(booking.status)


def _fee_snapshot(*, kind, counselor, on):
    """The amount to freeze, and whether it is billable at all.

    Returns ``(fee, cents, is_billable, reason)``. Two cases produce a session that
    is recorded and not charged for, and telling them apart matters:

      * **No fee configured.** The ministry has not decided. Recorded with a reason
        that says so, and surfaced on the billing index as work to do, because the
        alternative — inventing a number, or refusing to record the session — is
        either a wrong bill or a lost one.
      * **A fee of zero.** The ministry *has* decided, and the answer is nothing. A
        line for $0.00 on an invoice is noise, so it does not go on one.
    """
    fee = Fee.objects.resolve(kind=kind, counselor=counselor, on=on)
    if fee is None:
        return None, 0, False, _("No fee was set for this when the session was recorded.")
    if fee.amount_cents == 0:
        return fee, 0, False, _("The ministry does not charge for this.")
    return fee, fee.amount_cents, True, ""


def record_session(
    *,
    case,
    counselee,
    counselor=None,
    occurred_on=None,
    duration_minutes=60,
    kind=FeeKind.SESSION,
    booking=None,
    actor=None,
    request=None,
):
    """Create one billable record, with the fee that applies today frozen into it.

    ``counselor`` defaults to the one carrying the case, which is right for every
    session that came out of the diary. It is an argument so that a session
    delivered by somebody covering can be recorded as theirs.
    """
    counselor = counselor or case.counselor
    occurred_on = occurred_on or org_today()
    fee, cents, is_billable, reason = _fee_snapshot(kind=kind, counselor=counselor, on=occurred_on)

    session = SessionRecord.objects.create(
        booking=booking,
        case=case,
        counselor=counselor,
        counselee=counselee,
        occurred_on=occurred_on,
        duration_minutes=duration_minutes,
        kind=kind,
        fee=fee,
        fee_cents=cents,
        is_billable=is_billable,
        waived_reason=reason,
        # No schedule row matched, so the zero above is a gap and not a decision.
        needs_pricing=fee is None,
        recorded_by=actor,
    )
    record(
        AuditVerb.SESSION_RECORDED,
        actor=actor,
        target=session,
        request=request,
        case_id=case.pk,
        counselee_id=str(counselee.pk),
        kind=str(kind),
        fee_cents=cents,
        billable=is_billable,
        from_booking=booking.pk if booking is not None else None,
    )
    return session


def session_for_booking(booking, *, actor=None, request=None):
    """Make the billable record for a closed-out booking match its outcome.

    Called from ``scheduling.services`` every time a booking is completed, marked as
    a no-show, or cancelled — which is why it has to be idempotent and has to cope
    with a counselor correcting themselves. Three cases:

      * Nothing chargeable and no record yet: do nothing. Most cancellations.
      * Chargeable and no record: create one, at today's fee for the session's date.
      * A record already exists: re-price it **only if it is not yet on a live
        invoice**. Correcting an outcome after the bill has gone out cannot silently
        change the bill; the office voids it and raises another, and the log line
        below is what tells them to.

    Returns the SessionRecord, or None when there is nothing to bill.
    """
    kind = kind_for_booking(booking)
    existing = SessionRecord.objects.filter(booking=booking).first()

    if existing is None:
        if kind is None:
            return None
        return record_session(
            case=booking.case,
            counselee=booking.counselee,
            counselor=booking.counselor,
            occurred_on=booking.starts_at.astimezone(booking.counselor.zoneinfo).date(),
            duration_minutes=booking.duration_minutes,
            kind=kind,
            booking=booking,
            actor=actor,
            request=request,
        )

    if kind is None or kind == existing.kind:
        return existing

    if existing.is_invoiced:
        logger.warning(
            "Booking %s changed to %s but its session record %s is already on an "
            "invoice; the invoice has not been altered.",
            booking.pk,
            booking.status,
            existing.pk,
        )
        return existing

    fee, cents, is_billable, reason = _fee_snapshot(
        kind=kind, counselor=existing.counselor, on=existing.occurred_on
    )
    previous = existing.kind
    existing.kind = kind
    existing.fee = fee
    existing.fee_cents = cents
    existing.is_billable = is_billable
    existing.waived_reason = reason
    existing.needs_pricing = fee is None
    existing.save(
        update_fields=[
            "kind",
            "fee",
            "fee_cents",
            "is_billable",
            "waived_reason",
            "needs_pricing",
            "updated_at",
        ]
    )
    record(
        AuditVerb.SESSION_AMENDED,
        actor=actor,
        target=existing,
        request=request,
        case_id=existing.case_id,
        previous_kind=str(previous),
        kind=str(kind),
        fee_cents=cents,
        billable=is_billable,
        reason="the booking outcome changed",
    )
    return existing


def amend_session(session, *, fee_cents, is_billable, waived_reason="", actor, request=None):
    """Correct a recorded session's fee by hand.

    The path out of the commonest configuration mistake: sessions recorded before
    anybody set up the fee schedule, which come out at nothing and say so. Refuses
    once the session is on a live invoice, because the number is then something a
    counselee has been told.
    """
    if session.is_invoiced:
        raise AlreadyInvoiced(
            _("This session is on an invoice. Void the invoice to change what it says.")
        )
    if fee_cents < 0:
        raise BillingError(_("A fee cannot be negative."))
    if not is_billable and not waived_reason:
        raise BillingError(_("Say why this session is not being charged for."))

    before = (session.fee_cents, session.is_billable)
    session.fee_cents = fee_cents
    session.is_billable = is_billable
    session.waived_reason = "" if is_billable else waived_reason
    # The pointer into the schedule no longer explains the amount, so it is dropped
    # rather than left to imply that a rate said something it did not.
    session.fee = None
    # A person has now decided what this session is worth, including deciding it is
    # nothing. Either way it is off the billing page's list of work waiting.
    session.needs_pricing = False
    session.save(
        update_fields=[
            "fee",
            "fee_cents",
            "is_billable",
            "waived_reason",
            "needs_pricing",
            "updated_at",
        ]
    )
    record(
        AuditVerb.SESSION_AMENDED,
        actor=actor,
        target=session,
        request=request,
        case_id=session.case_id,
        previous_fee_cents=before[0],
        previous_billable=before[1],
        fee_cents=fee_cents,
        billable=is_billable,
        reason="corrected by hand",
    )
    return session


# --- drafting an invoice --------------------------------------------------


def recalculate_total(invoice):
    """Foot the invoice from its lines. The only thing that sets ``total_cents``."""
    total = sum(line.amount_cents for line in invoice.lines.all())
    if total != invoice.total_cents:
        invoice.total_cents = total
        invoice.save(update_fields=["total_cents", "updated_at"])
    return total


@transaction.atomic
def create_invoice(*, case, counselee, sessions=(), memo="", actor, request=None):
    """Draft an invoice for a counselee, optionally billing a set of sessions.

    Every session is checked rather than trusted: the form offers only the right
    ones, and a POST is not a form. A session belonging to another case, another
    payer, one that is not billable, or one already on an invoice that stands is a
    refusal — not a line silently dropped, because an invoice that quietly omits a
    session is how an hour goes unbilled for ever.

    A draft, not an invoice. It is a working document until ``issue_invoice`` puts a
    date on it, and until then the payer cannot see it at all.
    """
    sessions = list(sessions)
    for session in sessions:
        if session.case_id != case.pk or session.counselee_id != counselee.pk:
            raise BillingError(_("That session belongs to a different case or person."))
        if not session.is_billable:
            raise BillingError(
                _("%(what)s is not being charged for.") % {"what": session.line_description()}
            )
        if session.is_invoiced:
            raise AlreadyInvoiced(
                _("%(what)s is already on an invoice.") % {"what": session.line_description()}
            )

    invoice = Invoice.objects.create(
        number=next_invoice_number(),
        case=case,
        counselee=counselee,
        memo=memo[:200],
        raised_by=actor,
    )
    for session in sessions:
        InvoiceLineItem.objects.create(
            invoice=invoice,
            description=session.line_description(),
            quantity=1,
            unit_amount_cents=session.fee_cents,
            amount_cents=session.fee_cents,
            session=session,
        )
    recalculate_total(invoice)

    record(
        AuditVerb.INVOICE_CREATED,
        actor=actor,
        target=invoice,
        request=request,
        case_id=case.pk,
        number=invoice.number,
        counselee_id=str(counselee.pk),
        sessions=len(sessions),
        total_cents=invoice.total_cents,
    )
    return invoice


def add_line(invoice, *, description, unit_amount_cents, quantity=1, session=None, actor=None):
    """Put one charge on a draft. Refuses on anything already issued."""
    if not invoice.is_draft:
        raise NotEditable
    if quantity < 1:
        raise BillingError(_("A quantity has to be at least one."))
    if unit_amount_cents < 0:
        raise BillingError(_("An amount cannot be negative."))

    line = InvoiceLineItem.objects.create(
        invoice=invoice,
        description=description[:200],
        quantity=quantity,
        unit_amount_cents=unit_amount_cents,
        amount_cents=quantity * unit_amount_cents,
        session=session,
    )
    recalculate_total(invoice)
    return line


def remove_line(line, *, actor=None):
    """Take a charge off a draft.

    A real delete, unlike almost everything else in this project, and defensible for
    one reason: a draft has not been shown to anybody. Nothing was said, so there is
    nothing to keep a record of unsaying. The moment the invoice is issued this stops
    being possible.
    """
    invoice = line.invoice
    if not invoice.is_draft:
        raise NotEditable
    line.delete()
    recalculate_total(invoice)
    return invoice


@transaction.atomic
def issue_invoice(invoice, *, actor, due_on=None, request=None):
    """Send it. The moment the amount becomes something a counselee owes.

    Refuses an empty invoice and refuses one totalling nothing: a bill for $0.00
    tells the recipient nothing except that the office made a mistake.
    """
    if not invoice.is_draft:
        raise BillingError(_("That invoice has already been sent."))
    recalculate_total(invoice)
    if invoice.total_cents <= 0:
        raise BillingError(_("An invoice needs at least one charge on it before it can go out."))

    issued = org_today()
    invoice.status = InvoiceStatus.OPEN
    invoice.issued_on = issued
    invoice.issued_at = timezone.now()
    invoice.due_on = due_on or issued + timedelta(days=settings.BILLING_DUE_DAYS)
    invoice.save(update_fields=["status", "issued_on", "issued_at", "due_on", "updated_at"])

    record(
        AuditVerb.INVOICE_ISSUED,
        actor=actor,
        target=invoice,
        request=request,
        case_id=invoice.case_id,
        number=invoice.number,
        counselee_id=str(invoice.counselee_id),
        total_cents=invoice.total_cents,
        due_on=invoice.due_on.isoformat(),
    )
    # After the row is written and outside nothing: notify logs and swallows, so a
    # mail failure cannot roll back an invoice the office believes it has sent.
    notify.invoice_issued(invoice)
    return invoice


@transaction.atomic
def void_invoice(invoice, *, actor, reason, request=None):
    """Withdraw it, with a reason, keeping the row.

    Voided rather than deleted, and the reason is required rather than optional: an
    invoice that disappeared with no explanation is the first thing an auditor asks
    about, and the counselee can still see that it was withdrawn. Voiding also
    releases its sessions, so the corrected invoice can be raised from the same ones
    — see ``SessionRecordQuerySet.uninvoiced``.
    """
    # Re-read under a lock, as record_payment does, and for the sharper version of
    # the same reason: both checks below are against numbers a payment changes, and
    # the instance handed to us was loaded before it. A caller holding an invoice
    # from a moment ago must not be able to void one that has since been paid.
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == InvoiceStatus.VOID:
        return invoice
    if invoice.amount_paid_cents != 0:
        raise BillingError(
            _(
                "Money has been received against this invoice, so it cannot be voided. "
                "Refund the payment first."
            )
        )
    reason = (reason or "").strip()
    if not reason:
        raise BillingError(_("Say why this invoice is being withdrawn."))

    invoice.status = InvoiceStatus.VOID
    invoice.voided_at = timezone.now()
    invoice.voided_by = actor
    invoice.void_reason = reason[:200]
    invoice.save(update_fields=["status", "voided_at", "voided_by", "void_reason", "updated_at"])
    record(
        AuditVerb.INVOICE_VOIDED,
        actor=actor,
        target=invoice,
        request=request,
        case_id=invoice.case_id,
        number=invoice.number,
        # Staff-written billing text, not counseling content, so it is safe here —
        # unlike a cancellation reason, which is the counselee's own words.
        reason=invoice.void_reason,
        total_cents=invoice.total_cents,
    )
    return invoice


@transaction.atomic
def write_off_invoice(invoice, *, actor, reason, request=None):
    """Accept that it will not be paid.

    Kept distinct from voiding, and the distinction is the ministry's books rather
    than pedantry: voiding says the invoice was wrong, writing off says it was right.
    A written-off invoice still counts as owed and can still be paid, which is why
    it stays in ``PAYABLE_STATUSES`` — somebody who settles up a year later should
    not find their payment refused.
    """
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status not in PAYABLE_STATUSES:
        raise BillingError(_("Only an invoice that is awaiting payment can be written off."))
    reason = (reason or "").strip()
    if not reason:
        raise BillingError(_("Say why this is being written off."))

    invoice.status = InvoiceStatus.UNCOLLECTIBLE
    invoice.save(update_fields=["status", "updated_at"])
    record(
        AuditVerb.INVOICE_WRITTEN_OFF,
        actor=actor,
        target=invoice,
        request=request,
        case_id=invoice.case_id,
        number=invoice.number,
        reason=reason[:200],
        balance_cents=invoice.balance_cents,
    )
    return invoice


# --- money in --------------------------------------------------------------


def recalculate_payments(invoice):
    """Re-derive ``amount_paid_cents`` and the status from the payment rows.

    The only thing that writes either. Summing rather than incrementing is what makes
    a webhook and a staff member working on the same invoice at the same time safe:
    whichever commits second recomputes the whole answer instead of adding to a
    number it read before the other one wrote.

    The status follows the arithmetic. Paying an invoice that had been written off
    marks it paid, which is the right outcome — the money arrived.
    """
    paid = paid_total_cents(invoice)
    status = invoice.status
    if status in PAYABLE_STATUSES and paid >= invoice.total_cents:
        status = InvoiceStatus.PAID
    elif status == InvoiceStatus.PAID and paid < invoice.total_cents:
        # A reversal. Back to awaiting payment rather than to whatever it was
        # before, because "written off and then partly refunded" is not a state
        # anybody needs and "still owed" is the truth.
        status = InvoiceStatus.OPEN

    if (paid, status) != (invoice.amount_paid_cents, invoice.status):
        invoice.amount_paid_cents = paid
        invoice.status = status
        invoice.save(update_fields=["amount_paid_cents", "status", "updated_at"])
    return invoice


@transaction.atomic
def record_payment(
    invoice,
    *,
    amount_cents,
    method,
    received_on=None,
    reference="",
    actor=None,
    stripe_payment_intent_id="",
    request=None,
):
    """Record money received. Locks the invoice, writes the row, re-foots the total."""
    if amount_cents <= 0:
        raise BillingError(_("A payment has to be a positive amount."))
    # Re-read under a lock: the status check and the total below have to be made
    # against a row nobody else is changing at the same instant.
    invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status not in PAYABLE_STATUSES:
        raise BillingError(_("That invoice is not awaiting payment."))

    payment = Payment.objects.create(
        invoice=invoice,
        amount_cents=amount_cents,
        method=method,
        received_on=received_on or org_today(),
        reference=reference[:120],
        recorded_by=actor,
        stripe_payment_intent_id=stripe_payment_intent_id,
    )
    recalculate_payments(invoice)

    record(
        AuditVerb.PAYMENT_RECORDED,
        actor=actor,
        target=payment,
        request=request,
        case_id=invoice.case_id,
        invoice=invoice.number,
        amount_cents=amount_cents,
        method=str(method),
        stripe=bool(stripe_payment_intent_id),
        balance_cents=invoice.balance_cents,
    )
    return payment


@transaction.atomic
def reverse_payment(payment, *, actor, reason="", request=None):
    """Undo a payment by writing its opposite, never by editing it.

    A bounced check and a card refund are both this. The original row stays exactly
    as it was, because what the office banked in March is a fact even after the bank
    took it back in April.
    """
    if payment.is_reversal:
        raise BillingError(_("That is already a reversal."))
    # Asked of the database rather than of the instance. ``hasattr(payment,
    # "reversed_by")`` would look at a reverse-one-to-one cache that Django fills in
    # with a negative result the first time it is read, so a caller holding an
    # instance from before the first reversal would be told there was none and would
    # write a second one. The OneToOne would catch it, but as an IntegrityError with
    # nothing to show the person who clicked.
    if Payment.objects.filter(reverses=payment).exists():
        raise BillingError(_("That payment has already been reversed."))

    invoice = Invoice.objects.select_for_update().get(pk=payment.invoice_id)
    reversal = Payment.objects.create(
        invoice=invoice,
        amount_cents=-payment.amount_cents,
        method=payment.method,
        received_on=org_today(),
        reference=(reason or _("Reversal"))[:120],
        recorded_by=actor,
        reverses=payment,
    )
    recalculate_payments(invoice)

    record(
        AuditVerb.PAYMENT_REVERSED,
        actor=actor,
        target=reversal,
        request=request,
        case_id=invoice.case_id,
        invoice=invoice.number,
        amount_cents=reversal.amount_cents,
        reverses=payment.pk,
        reason=(reason or "")[:200],
        balance_cents=invoice.balance_cents,
    )
    return reversal


@transaction.atomic
def apply_stripe_payment(invoice, *, payment_intent_id, amount_cents, reference=""):
    """Record a card payment that Stripe says has been taken.

    ``actor`` is deliberately absent — nobody in this ministry did this, Stripe did,
    and the audit row's null actor says exactly that, the same way the reminder job's
    does.

    Idempotent by an explicit check as well as by the unique index, because arriving
    here twice is normal rather than exceptional: Stripe retries a delivery it did
    not get a prompt answer to, and the reconciliation command may reach the same
    charge from the other direction. Returns the existing payment when there is one.
    """
    if payment_intent_id:
        existing = Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).first()
        if existing is not None:
            logger.info(
                "Stripe payment %s is already recorded as payment %s; ignoring.",
                payment_intent_id,
                existing.pk,
            )
            return existing

    return record_payment(
        invoice,
        amount_cents=amount_cents,
        method=PaymentMethod.CARD,
        reference=reference[:120],
        actor=None,
        stripe_payment_intent_id=payment_intent_id,
    )


# --- reminders -------------------------------------------------------------


def due_invoice_reminders(*, on=None):
    """Overdue invoices nobody has been nudged about yet.

    Overdue, not merely open: an invoice with three weeks left to run does not need
    an email. And one nudge per invoice for ever, which the ``reminder_sent_at is
    null`` filter is what enforces — see the command for why this is not a dunning
    sequence.
    """
    return (
        Invoice.objects.overdue(on=on)
        .filter(reminder_sent_at__isnull=True)
        .select_related("counselee", "case")
        .order_by("due_on")
    )


def send_invoice_reminder(invoice, *, now=None) -> bool:
    """Send one reminder, returning whether this caller sent it.

    Claimed with a conditional UPDATE before the mail goes out, the same shape as
    ``scheduling.services.send_reminder`` and ``LoginToken.consume``: two overlapping
    cron runs would otherwise both pass the ``reminder_sent_at is null`` filter above
    and email the same counselee twice about the same bill.
    """
    now = now or timezone.now()
    claimed = Invoice.objects.filter(pk=invoice.pk, reminder_sent_at__isnull=True).update(
        reminder_sent_at=now, updated_at=now
    )
    if not claimed:
        return False

    invoice.reminder_sent_at = now
    notify.invoice_reminder(invoice)
    record(
        AuditVerb.INVOICE_REMINDER_SENT,
        actor=None,
        target=invoice,
        case_id=invoice.case_id,
        invoice=invoice.number,
        balance_cents=invoice.balance_cents,
        due_on=invoice.due_on.isoformat() if invoice.due_on else None,
    )
    return True


# --- what the billing page needs ------------------------------------------


def uninvoiced_work(*, actor):
    """Billable sessions nobody has raised an invoice for, grouped by who owes.

    Grouped by ``(case, counselee)`` rather than by case, because an invoice is
    addressed to one person: on a couple's case each spouse gets their own bill, and
    a page that offered "invoice this case" would produce one addressed to whichever
    of them the code happened to pick.

    Scoped through ``for_actor`` like everything else, even though only billing and
    an administrator can reach the page — a view that reaches for ``objects`` because
    "only staff see this anyway" is how the next role added to the ministry gets more
    than it was given.
    """
    sessions = (
        SessionRecord.objects.for_actor(actor)
        .uninvoiced()
        .select_related("case", "case__counselor", "counselee")
        .order_by("case__label", "counselee__last_name", "occurred_on")
    )
    groups = {}
    for session in sessions:
        key = (session.case_id, session.counselee_id)
        group = groups.setdefault(
            key,
            {
                "case": session.case,
                "counselee": session.counselee,
                "sessions": [],
                "total_cents": 0,
            },
        )
        group["sessions"].append(session)
        group["total_cents"] += session.fee_cents
    return list(groups.values())


def unpriced_sessions(*, actor):
    """Sessions recorded with no fee because the schedule had nothing to say.

    Surfaced on the billing page as work to do. Without this they are simply absent
    from every list of what is owed, which is the quietest way for a ministry to stop
    charging for its counseling.

    Keyed on ``needs_pricing`` rather than on "not billable and no fee row", which
    would also catch every session the office deliberately waived and so leave the
    list impossible to clear.
    """
    return (
        SessionRecord.objects.for_actor(actor)
        .filter(needs_pricing=True)
        .select_related("case", "counselee", "counselor")
        .order_by("-occurred_on")
    )
