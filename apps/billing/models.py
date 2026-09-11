"""
Sessions, invoices, and payments.

The chain is short and it only runs one way:

    Booking --closed out--> SessionRecord --a line on--> Invoice <--applied to-- Payment

**A SessionRecord is the billable fact, and it is not the Booking.** A booking is
an intention: it can be moved, cancelled, or rescheduled, and a counselor may
correct an outcome a week later. An invoice cannot follow all that around. So the
moment a booking is closed out — held, missed, or cancelled too late to fill the
hour — the fact of it is copied here, with **the fee that applied on the day frozen
into the row**. ``fee_cents`` is never recomputed. A ministry that raises its rates
in March must not silently reprice February, and an invoice reissued next year has
to say what it said at the time.

That is also why a ``SessionRecord`` can exist with no booking at all. A session
held at short notice in the church office, or a phone call the counselor agreed to
bill for, is a session; the diary is how most of them get recorded, not what makes
them real.

**Money is whole cents, everywhere.** See apps/billing/money.py.

**Nothing here carries counseling content, and that is the design.** There is no
notes field on a SessionRecord and no description of what was discussed anywhere in
this app, because ``financial_admin`` can read all of it. The booking's
``counselor_note`` stays on the booking, behind ``scheduling.view_booking_note``,
which billing does not hold. An invoice line says "Counseling session, 12 March" and
that is the most it will ever say.

**One divergence from the rest of the application, stated loudly.**
``InvoiceQuerySet.scope_for_counselee`` matches on ``counselee=user`` and does *not*
require a current ``CaseMember`` row, unlike documents, messages, and bookings. A
bill you cannot see is a bill you cannot pay, and counseling ending is the moment a
final invoice is most likely to be outstanding. It is safe here precisely because of
the paragraph above: an invoice carries no counseling content by construction, so
there is nothing for a lapsed membership to protect. The same reasoning applies to
``SessionRecord``, which is what the lines are drawn from.

**Invoices are voided, never deleted, and an issued one is immutable.** The lines on
an open invoice cannot change: a counselee who has been told they owe $340 must not
find a different number there tomorrow. A mistake is corrected by voiding the
invoice, with a reason, and raising another. That is why every ``on_delete`` in this
module is ``PROTECT``.
"""

from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role
from apps.billing import money
from apps.core.dates import org_today
from apps.core.models import TimeStampedModel
from apps.core.scoping import ActorScopedQuerySet, CaseScopedQuerySet


def default_currency() -> str:
    """A module-level function because a field default has to be importable by a
    migration — the same reason ``org_today`` is one."""
    return money.currency_code()


class FeeKind(models.TextChoices):
    """What is being charged for.

    Three kinds rather than one, because the ministry bills them at different
    rates and sometimes at nothing: a no-show may cost the full session where a
    late cancellation costs half, or neither may be charged at all. Keeping them
    apart means that decision is a row in the fee schedule instead of a
    conversation each time.
    """

    SESSION = "session", _("Counseling session")
    LATE_CANCELLATION = "late_cancellation", _("Late cancellation")
    NO_SHOW = "no_show", _("Missed appointment")


# --- the fee schedule -----------------------------------------------------


class FeeQuerySet(ActorScopedQuerySet):
    """Ministry configuration, readable by the two roles that set it.

    A counselor gets nothing, which is deliberate and worth defending: what the
    ministry charges is not the counselor's decision, and a rate list that
    included their colleagues' would be a disclosure about how other people are
    paid. A counselor who needs to know what a session costs can ask, or read an
    invoice on their own case.

    A counselee gets nothing either, for a plainer reason: what *they* owe is on
    their invoice, itemised, with the fee that actually applied. The schedule is a
    list of hypotheticals, some of which are other people's.
    """

    def scope_for_admin(self, user):
        return self

    def scope_for_financial_admin(self, user):
        return self

    def in_force(self, *, on=None):
        on = on or org_today()
        return self.filter(effective_from__lte=on).filter(
            models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=on)
        )


class FeeManager(models.Manager.from_queryset(FeeQuerySet)):
    def for_actor(self, user):
        return self.get_queryset().for_actor(user)

    def resolve(self, *, kind, counselor=None, on=None):
        """The fee that applies, or None if the ministry has not set one.

        Most specific wins: a rate set for this counselor beats the ministry
        default. Within one specificity the latest ``effective_from`` wins, which
        is how a rate change is made — a new row from the date it takes effect,
        leaving the old one in place so that an invoice raised for an older session
        can still be explained.

        Returns ``None`` rather than raising or defaulting to zero. A missing fee
        is a configuration gap that a person has to close, and
        ``services.record_session`` records the session as not billable and says so
        on the billing page instead of inventing a number.
        """
        on = on or org_today()
        candidates = self.get_queryset().in_force(on=on).filter(kind=kind)
        if counselor is not None:
            specific = (
                candidates.filter(counselor=counselor).order_by("-effective_from", "-id").first()
            )
            if specific is not None:
                return specific
        return candidates.filter(counselor__isnull=True).order_by("-effective_from", "-id").first()


class Fee(TimeStampedModel):
    """One rate, for one kind of charge, in force over a range of dates."""

    kind = models.CharField(max_length=30, choices=FeeKind.choices)
    # Null means "the ministry's rate", which is what almost every row will be. A
    # counselor named here overrides it for their own sessions — a supervisor whose
    # time is charged differently, or a counselor in training whose sessions are
    # free while they are being observed.
    counselor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="fees",
        limit_choices_to={"role": Role.COUNSELOR},
        help_text=_("Leave blank for the ministry's standard rate."),
    )
    amount_cents = models.IntegerField(
        help_text=_("Zero is allowed and means this is not charged for."),
    )

    # org_today, not localdate: a rate takes effect on a day of the ministry's
    # calendar, not of whoever typed it in. See apps/core/dates.py.
    effective_from = models.DateField(default=org_today)
    effective_to = models.DateField(
        null=True,
        blank=True,
        help_text=_("Leave blank while this rate is current."),
    )

    objects = FeeManager()

    class Meta:
        ordering = ["kind", "counselor", "-effective_from"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__gte=0), name="fee_is_not_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(effective_to__isnull=True)
                | models.Q(effective_to__gte=models.F("effective_from")),
                name="fee_effective_range_is_ordered",
            ),
            # Two constraints rather than one, because Postgres treats NULLs as
            # distinct and a plain unique over (kind, counselor, effective_from)
            # would happily accept two ministry defaults for the same day. The same
            # shape as uniq_all_day_override_per_date in scheduling.
            models.UniqueConstraint(
                fields=["kind", "counselor", "effective_from"],
                condition=models.Q(counselor__isnull=False),
                name="uniq_counselor_fee_per_date",
            ),
            models.UniqueConstraint(
                fields=["kind", "effective_from"],
                condition=models.Q(counselor__isnull=True),
                name="uniq_ministry_fee_per_date",
            ),
        ]
        indexes = [models.Index(fields=["kind", "effective_from"])]

    def __str__(self) -> str:
        who = self.counselor.full_name if self.counselor_id else _("ministry standard")
        return f"{self.get_kind_display()}, {who}: {money.format_cents(self.amount_cents)}"

    @property
    def is_current(self) -> bool:
        today = org_today()
        if self.effective_from > today:
            return False
        return self.effective_to is None or self.effective_to >= today

    def display_amount(self) -> str:
        return money.format_cents(self.amount_cents)


# --- what happened ---------------------------------------------------------


class SessionRecordQuerySet(CaseScopedQuerySet):
    case_path = "case"

    def scope_for_counselee(self, user):
        """Their own sessions, whether or not the membership is still live.

        The divergence the module docstring explains. Deliberately not
        ``super().scope_for_counselee``, which would require a current
        ``CaseMember`` and so hide the sessions behind a final invoice from the
        person who owes it.
        """
        return self.filter(counselee=user)

    def billable(self):
        return self.filter(is_billable=True)

    def uninvoiced(self):
        """Billable sessions that are not already on an invoice that stands.

        A line on a *voided* invoice does not count, which is what makes "void it
        and raise another" work: the sessions become available again the moment the
        mistake is withdrawn.
        """
        on_a_live_invoice = InvoiceLineItem.objects.filter(
            session=models.OuterRef("pk"),
        ).exclude(invoice__status=InvoiceStatus.VOID)
        return self.billable().filter(~models.Exists(on_a_live_invoice))


class SessionRecordManager(models.Manager.from_queryset(SessionRecordQuerySet)):
    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class SessionRecord(TimeStampedModel):
    """One billable event, with the fee that applied to it frozen in.

    Deliberately **no notes field of any kind**. ``financial_admin`` reads every
    row in this table, and the moment there is somewhere here to write "she
    disclosed the affair" somebody eventually will. What the session was about
    lives on the booking, behind a permission billing does not hold.
    """

    # Null for a session that never went through the diary. OneToOne rather than a
    # plain FK so one booking can raise exactly one billable record — the
    # database's own guarantee that correcting an outcome twice does not bill the
    # hour twice.
    booking = models.OneToOneField(
        "scheduling.Booking",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="session_record",
    )
    case = models.ForeignKey(
        "counseling.Case", on_delete=models.PROTECT, related_name="session_records"
    )
    # Copied from the booking rather than read through it, because a case can be
    # reassigned and the record of who was in the room must not move with it.
    counselor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sessions_delivered",
        limit_choices_to={"role": Role.COUNSELOR},
    )
    counselee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="sessions_received",
        limit_choices_to={"role": Role.COUNSELEE},
        help_text=_("Who the session was for, and who is billed for it."),
    )

    # A date, not a timestamp. Which day the session falls on for billing is a
    # fact about the ministry's month; the exact instant is on the booking.
    occurred_on = models.DateField(default=org_today)
    duration_minutes = models.PositiveSmallIntegerField(default=60)
    kind = models.CharField(max_length=30, choices=FeeKind.choices, default=FeeKind.SESSION)

    #: **A snapshot, never recomputed.** See the module docstring.
    fee_cents = models.IntegerField(default=0)
    # Which schedule row it came from, kept for "why does this say $85". SET_NULL
    # rather than PROTECT: the amount is already frozen above, so losing the
    # pointer costs an explanation and not a number.
    fee = models.ForeignKey(
        Fee, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions"
    )

    is_billable = models.BooleanField(
        default=True,
        help_text=_("Untick for a session the ministry is not charging for."),
    )
    # Why not, in words the counselee could read if it ever reached an invoice —
    # "waived", "covered by the benevolence fund", "no fee configured". Not a place
    # for anything about the counseling.
    waived_reason = models.CharField(max_length=200, blank=True)

    # True when the fee schedule had nothing to say, so the zero on this row is a
    # placeholder rather than a decision. Stored rather than inferred because the two
    # cases look identical on the row — both are not billable, with a reason — and
    # need opposite handling: "nobody has set a rate" is work waiting on the billing
    # page, "the ministry is not charging for this" is finished business. Inferring it
    # from ``fee is None`` would put every deliberately waived session back on the
    # to-do list for ever, with no way to clear it.
    needs_pricing = models.BooleanField(default=False)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sessions_recorded",
        help_text=_("Null when the record was raised automatically from a booking."),
    )

    objects = SessionRecordManager()

    class Meta:
        ordering = ["-occurred_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(fee_cents__gte=0), name="session_fee_is_not_negative"
            ),
            # A session that is not being charged for must say why. Enforced in the
            # database because the alternative is an invoice with a missing line and
            # nobody able to say whether that was a decision or a bug.
            models.CheckConstraint(
                condition=models.Q(is_billable=True) | ~models.Q(waived_reason=""),
                name="an_unbilled_session_says_why",
            ),
            models.CheckConstraint(
                condition=models.Q(duration_minutes__gt=0),
                name="session_has_a_duration",
            ),
        ]
        indexes = [
            models.Index(fields=["case", "-occurred_on"]),
            models.Index(fields=["counselee", "-occurred_on"]),
            models.Index(fields=["counselor", "-occurred_on"]),
            models.Index(fields=["is_billable", "-occurred_on"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} on {self.occurred_on}"

    @property
    def is_invoiced(self) -> bool:
        """On an invoice that has not been voided."""
        return self.invoice_lines.exclude(invoice__status=InvoiceStatus.VOID).exists()

    def display_fee(self) -> str:
        return money.format_cents(self.fee_cents)

    def line_description(self) -> str:
        """What this looks like on an invoice.

        Says the kind and the date and stops. A counselee's own name is not on it —
        the invoice is addressed to them — and neither is anything else.
        """
        return f"{self.get_kind_display()}, {self.occurred_on:%d %b %Y}"


# --- invoices --------------------------------------------------------------


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    OPEN = "open", _("Sent, awaiting payment")
    PAID = "paid", _("Paid")
    VOID = "void", _("Voided")
    UNCOLLECTIBLE = "uncollectible", _("Written off")


#: Statuses a payment can be applied to. A draft has not been sent to anybody and
#: a void one has been withdrawn, so money arriving against either is a mistake
#: worth refusing rather than absorbing.
PAYABLE_STATUSES = (InvoiceStatus.OPEN, InvoiceStatus.UNCOLLECTIBLE)


class InvoiceQuerySet(CaseScopedQuerySet):
    case_path = "case"

    def scope_for_counselee(self, user):
        """Their own bills, and only theirs — including after counseling ends.

        The divergence, explained at the top of this module. On a couple's case
        each spouse sees the invoices addressed to them and nothing of the other's,
        which is the same "nothing of the other's" rule everywhere else; the
        difference is only that a membership ending does not take it away.
        """
        return self.filter(counselee=user)

    def readable_by(self, user):
        """Drop the rows ``billing.view_invoice`` would refuse this actor.

        For a **list**, which is where scoping and permissions have to meet. A draft
        is in the payer's queryset on purpose — it is their bill, and the scoping
        layer's question is only whose — but the permission layer refuses to show
        them an amount the office is still working out. A list that ignored that
        would advertise the number in its own total and offer a link that answers
        403.

        One definition, used by every page that lists invoices, because the same
        exclusion written out at each call site is the kind that ends up written at
        all but one.
        """
        if getattr(user, "role", None) == Role.COUNSELEE:
            return self.exclude(status=InvoiceStatus.DRAFT)
        return self

    def outstanding(self):
        return self.filter(status=InvoiceStatus.OPEN)

    def overdue(self, *, on=None):
        on = on or org_today()
        return self.outstanding().filter(due_on__lt=on)


class InvoiceManager(models.Manager.from_queryset(InvoiceQuerySet)):
    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class Invoice(TimeStampedModel):
    """One bill, addressed to one counselee, for sessions on one case."""

    # The reference a counselee quotes on a check. Assigned from a Postgres
    # sequence when the row is created — see apps/billing/numbering.py, including
    # why a gap in the run is not a missing invoice.
    number = models.CharField(max_length=32, unique=True, editable=False)

    case = models.ForeignKey("counseling.Case", on_delete=models.PROTECT, related_name="invoices")
    counselee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="invoices",
        limit_choices_to={"role": Role.COUNSELEE},
        help_text=_("Who is being billed. One invoice is addressed to one person."),
    )

    status = models.CharField(
        max_length=20, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT, db_index=True
    )

    # Null while it is a draft. Set once, when it is issued: this is the date the
    # invoice bears, and it must not move afterwards.
    issued_on = models.DateField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)

    # Snapshotted per invoice rather than read from settings at render time, so an
    # invoice from before a currency change still says what it said.
    currency = models.CharField(max_length=3, default=default_currency, editable=False)

    # Maintained by services.recalculate_total from the lines, which are immutable
    # once the invoice is issued. Denormalised because every list of invoices needs
    # it and a page of them would otherwise be a page of aggregate queries.
    total_cents = models.IntegerField(default=0)
    amount_paid_cents = models.IntegerField(default=0)

    # Shown to the payer, so it is not the place for anything about the
    # counseling. "Sessions for March", "as agreed with the office".
    memo = models.CharField(max_length=200, blank=True)

    raised_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices_raised",
    )
    issued_at = models.DateTimeField(null=True, blank=True)

    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoices_voided",
    )
    # Required when voiding. An invoice that vanished with no explanation is the
    # thing an auditor asks about first.
    void_reason = models.CharField(max_length=200, blank=True)

    # Stripe's own ids for this bill's payment attempt. Empty when the ministry
    # has not connected Stripe, which is a supported way to run: invoices still go
    # out and payments are recorded by hand.
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True)

    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    objects = InvoiceManager()

    class Meta:
        ordering = ["-issued_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total_cents__gte=0), name="invoice_total_is_not_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(amount_paid_cents__gte=0),
                name="invoice_paid_is_not_negative",
            ),
            # An invoice somebody has been asked to pay has a date on it. A draft
            # does not, and a voided draft never got one.
            models.CheckConstraint(
                condition=~models.Q(
                    status__in=[
                        InvoiceStatus.OPEN,
                        InvoiceStatus.PAID,
                        InvoiceStatus.UNCOLLECTIBLE,
                    ]
                )
                | models.Q(issued_on__isnull=False),
                name="an_issued_invoice_has_a_date",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=InvoiceStatus.VOID)
                | (models.Q(voided_at__isnull=False) & ~models.Q(void_reason="")),
                name="a_void_invoice_says_when_and_why",
            ),
            models.CheckConstraint(
                condition=models.Q(due_on__isnull=True)
                | models.Q(issued_on__isnull=True)
                | models.Q(due_on__gte=models.F("issued_on")),
                name="invoice_is_not_due_before_it_is_issued",
            ),
        ]
        indexes = [
            models.Index(fields=["case", "-issued_on"]),
            models.Index(fields=["counselee", "status"]),
            models.Index(fields=["status", "due_on"]),
        ]

    def __str__(self) -> str:
        return self.number

    @property
    def balance_cents(self) -> int:
        """What is still owed. Negative means an overpayment.

        Not clamped at zero on purpose: somebody paying a round number against a
        $337.50 bill is common, and a credit the office can see is better than a
        difference that quietly disappears.
        """
        return self.total_cents - self.amount_paid_cents

    @property
    def is_draft(self) -> bool:
        return self.status == InvoiceStatus.DRAFT

    @property
    def is_payable(self) -> bool:
        return self.status in PAYABLE_STATUSES and self.balance_cents > 0

    @property
    def is_overdue(self) -> bool:
        if self.status != InvoiceStatus.OPEN or self.due_on is None:
            return False
        return self.due_on < org_today()

    def display_total(self) -> str:
        return money.format_cents(self.total_cents, currency=self.currency)

    def display_paid(self) -> str:
        return money.format_cents(self.amount_paid_cents, currency=self.currency)

    def display_balance(self) -> str:
        return money.format_cents(self.balance_cents, currency=self.currency)


class InvoiceLineItemQuerySet(ActorScopedQuerySet):
    def for_actor(self, user):
        """The invoice decides, and it is never restated.

        The same construction as ``Participant`` and ``Message`` in messaging: a
        line has no audience of its own, so writing the rule twice is how the two
        come to disagree. The anonymous and inactive checks happen inside
        ``Invoice.objects.for_actor``, which returns ``none()`` for both.
        """
        return self.filter(invoice__in=Invoice.objects.for_actor(user))


class InvoiceLineItem(TimeStampedModel):
    """One charge on one invoice.

    ``amount_cents`` is stored as well as derivable, and a CheckConstraint asserts
    it equals ``quantity * unit_amount_cents``. That looks redundant and is not:
    the stored value is what the invoice was footed from, and the constraint is
    what stops a future migration or a hand-written UPDATE leaving a total that
    does not add up.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    # What the counselee reads. Built by SessionRecord.line_description for a
    # session line, or typed by the office for anything else.
    description = models.CharField(max_length=200)
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_amount_cents = models.IntegerField()
    amount_cents = models.IntegerField()

    # PROTECT, not SET_NULL: the session is the evidence for the charge, and a bill
    # whose lines point at nothing cannot be explained to the person paying it.
    session = models.ForeignKey(
        SessionRecord,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="invoice_lines",
    )

    objects = models.Manager.from_queryset(InvoiceLineItemQuerySet)()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="line_quantity_is_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    amount_cents=models.F("quantity") * models.F("unit_amount_cents")
                ),
                name="line_total_is_quantity_times_unit",
            ),
            # One session cannot appear twice on the same invoice. Across invoices
            # it is prevented by SessionRecordQuerySet.uninvoiced and by
            # services.create_invoice, which is application logic; this is the part
            # the database can hold on its own.
            models.UniqueConstraint(
                fields=["invoice", "session"],
                condition=models.Q(session__isnull=False),
                name="uniq_session_per_invoice",
            ),
        ]
        indexes = [models.Index(fields=["invoice", "id"])]

    def __str__(self) -> str:
        return f"{self.description} — {money.format_cents(self.amount_cents)}"

    def display_amount(self) -> str:
        return money.format_cents(self.amount_cents, currency=self.invoice.currency)

    def display_unit_amount(self) -> str:
        return money.format_cents(self.unit_amount_cents, currency=self.invoice.currency)


# --- payments --------------------------------------------------------------


class PaymentMethod(models.TextChoices):
    CARD = "card", _("Card")
    CASH = "cash", _("Cash")
    CHECK = "check", _("Check")
    BANK_TRANSFER = "bank_transfer", _("Bank transfer")


class PaymentQuerySet(ActorScopedQuerySet):
    def for_actor(self, user):
        """The invoice decides, as with a line item."""
        return self.filter(invoice__in=Invoice.objects.for_actor(user))


class Payment(TimeStampedModel):
    """Money received against an invoice.

    Not soft-deleted, and never edited. A payment recorded in error is corrected by
    ``services.reverse_payment``, which writes a second row carrying the negative
    amount rather than altering the first. The reason is the one that makes an audit
    row append-only: what the ministry believed on the day it banked the check is
    itself part of the record, and a bounced check is two events rather than one
    that never happened.
    """

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    amount_cents = models.IntegerField()
    method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    received_on = models.DateField(default=org_today)
    # A check number, a Stripe receipt reference, "envelope in the Sunday
    # collection". Free text, read by the office when something does not tie up.
    reference = models.CharField(max_length=120, blank=True)

    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payments_recorded",
        help_text=_("Null for a payment that arrived through Stripe."),
    )

    # Stripe's PaymentIntent, when the money came that way. Unique **when
    # present**, which is the second line of defence against a redelivered webhook
    # posting the same money twice — the first being the StripeEvent table. Two
    # independent guards because "we charged them twice" is the failure this app
    # can make that a counselee would find hardest to forgive.
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)

    #: Set on a row that reverses an earlier one, so a bounced check or a refunded
    #: card payment leaves both facts in place.
    reverses = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversed_by",
    )

    objects = models.Manager.from_queryset(PaymentQuerySet)()

    class Meta:
        ordering = ["-received_on", "-id"]
        constraints = [
            # Nonzero rather than positive: a reversal is stored as a negative
            # amount so that the invoice's paid total is the plain sum of this
            # column and nothing has to know about signs.
            models.CheckConstraint(condition=~models.Q(amount_cents=0), name="payment_is_not_zero"),
            models.CheckConstraint(
                condition=models.Q(reverses__isnull=True) | models.Q(amount_cents__lt=0),
                name="a_reversal_is_negative",
            ),
            models.UniqueConstraint(
                fields=["stripe_payment_intent_id"],
                condition=~models.Q(stripe_payment_intent_id=""),
                name="uniq_stripe_payment_intent",
            ),
        ]
        indexes = [models.Index(fields=["invoice", "-received_on"])]

    def __str__(self) -> str:
        return f"{money.format_cents(self.amount_cents)} on {self.received_on}"

    def display_amount(self) -> str:
        return money.format_cents(self.amount_cents, currency=self.invoice.currency)

    @property
    def is_reversal(self) -> bool:
        return self.reverses_id is not None


def paid_total_cents(invoice) -> int:
    """The sum of every payment row, reversals included.

    One definition, used by ``services.recalculate_payments`` and by the
    reconciliation command, so the number on the page and the number the nightly
    check compares against Stripe cannot come from two different sums.
    """
    return Payment.objects.filter(invoice=invoice).aggregate(
        total=Coalesce(models.Sum("amount_cents"), 0)
    )["total"]


# --- Stripe ----------------------------------------------------------------


class StripeCustomerQuerySet(ActorScopedQuerySet):
    """Who has a customer record at Stripe.

    Billing and an administrator, plus the counselee's own row. A counselor gets
    nothing: whether the ministry has told a payment processor about someone is not
    a counseling question, and it is one of the few facts in this system that has
    left the building.
    """

    def scope_for_admin(self, user):
        return self

    def scope_for_financial_admin(self, user):
        return self

    def scope_for_counselee(self, user):
        return self.filter(counselee=user)


class StripeCustomer(TimeStampedModel):
    """The link between a counselee and their record at Stripe.

    **Only an email address is ever sent.** Not the name, not the case, not the
    counselor, and nothing about the counseling. Creating one is a disclosure to a
    third party, so it is recorded with ``record_or_raise`` — if we cannot log that
    a counselee's address left the building, we do not send it. See
    ``apps/billing/stripe/client.py``.
    """

    counselee = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="stripe_customer",
        limit_choices_to={"role": Role.COUNSELEE},
    )
    stripe_customer_id = models.CharField(max_length=255, unique=True)
    # What we sent, so a later change of address is visible as a difference rather
    # than discovered when a receipt goes to an old inbox.
    email = models.EmailField()

    objects = models.Manager.from_queryset(StripeCustomerQuerySet)()

    class Meta:
        verbose_name = _("Stripe customer")

    def __str__(self) -> str:
        return self.stripe_customer_id


class StripeEventOutcome(models.TextChoices):
    PROCESSED = "processed", _("Processed")
    IGNORED = "ignored", _("Ignored — not an event we act on")
    DUPLICATE = "duplicate", _("Duplicate delivery")
    FAILED = "failed", _("Failed")


class StripeEvent(models.Model):
    """One webhook delivery, recorded so a redelivery cannot post twice.

    Stripe retries. It retries an event we handled successfully but answered too
    slowly, and it retries after a deploy that dropped the connection mid-request.
    Without this table a retried ``checkout.session.completed`` would record the
    same payment again, and the counselee's invoice would show a credit that never
    existed.

    The **payload is deliberately not stored.** It would be the one copy of a
    counselee's billing data in this database that no access rule governs, sitting
    in a JSON column that every ``financial_admin`` page could accidentally render.
    The event id, the type, and what we did with it are enough to answer every
    question this table exists for; the payload itself is in Stripe's dashboard,
    which is where an investigation should be looking anyway.

    Not a ``TimeStampedModel``: ``received_at`` is the creation time and a second
    column saying so would be one more thing to disagree with it.
    """

    stripe_event_id = models.CharField(max_length=255, unique=True)
    event_type = models.CharField(max_length=100)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=StripeEventOutcome.choices, blank=True)
    # One line, for a person reading the list. Never the payload.
    detail = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-received_at", "-id"]
        verbose_name = _("Stripe event")

    def __str__(self) -> str:
        return f"{self.event_type} {self.stripe_event_id}"
