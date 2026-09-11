"""
Billing forms.

All plain ``Form`` rather than ``ModelForm``, for the reason messaging's are: a
ModelForm over ``Invoice`` would let a field appear in a template that sets
``status``, ``number``, or ``amount_paid_cents``, and none of those is something a
browser gets to state. Every one of them is set by ``services``.

**Amounts are typed in units and stored in cents.** These forms are one of the two
edges ``money.py`` talks about: a ``DecimalField`` with two decimal places is what a
person types into, ``money.to_cents`` is the only conversion, and nothing downstream
of ``cleaned_data`` sees a decimal again.

**A queryset in a choice field is a security control, not a convenience.** The
sessions offered on the invoice form are the uninvoiced ones on this case, and the
counselee offered is somebody on this case, because an id outside a field's queryset
has to be a validation error rather than an invoice raised against another
counselor's counselee. ``services`` re-checks all of it — that is the control, this
is the courtesy — but both are cheap and neither is sufficient alone.
"""

from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role, User
from apps.billing import money
from apps.billing.models import FeeKind, PaymentMethod, SessionRecord
from apps.core.dates import org_today


def _amount_field(label, **kwargs):
    """The one way an amount is asked for.

    ``max_digits`` allows up to $99,999.99, which is far more than a counseling
    session and far less than an integer overflow. ``min_value`` of zero rather than
    of a cent: a fee of nothing is a real answer, and the fee schedule says so.
    """
    kwargs.setdefault("min_value", 0)
    kwargs.setdefault("decimal_places", 2)
    kwargs.setdefault("max_digits", 9)
    return forms.DecimalField(label=label, **kwargs)


class FeeForm(forms.Form):
    """Add a rate to the schedule.

    There is no form for *editing* a rate, and that is deliberate. A rate is a
    historical fact — invoices were raised against it — so a change is a new row from
    the date it takes effect, and the old one is ended rather than overwritten. See
    ``FeeManager.resolve``.
    """

    kind = forms.ChoiceField(choices=FeeKind.choices, label=_("What this is for"))
    counselor = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        label=_("Only for this counselor"),
        help_text=_("Leave blank for the ministry's standard rate."),
    )
    amount = _amount_field(
        _("Amount"),
        help_text=_("Enter 0 for something the ministry does not charge for."),
    )
    effective_from = forms.DateField(
        initial=org_today,
        widget=forms.DateInput(attrs={"type": "date"}),
        label=_("In force from"),
        help_text=_("Sessions on or after this date use this rate."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["counselor"].queryset = User.objects.filter(
            role=Role.COUNSELOR, is_active=True
        ).order_by("last_name", "first_name")
        self.fields["counselor"].label_from_instance = lambda user: user.full_name

    def amount_cents(self) -> int:
        return money.to_cents(self.cleaned_data["amount"])


class InvoiceCreateForm(forms.Form):
    """Draft an invoice for one person on one case.

    The counselee comes first because it decides everything else. The session list
    shows every uninvoiced session on the case with whose it is on the label rather
    than filtering as the choice changes: this application has no JavaScript build
    step and a page that needed one to be correct would be a page that is wrong with
    it switched off. ``clean`` catches a mismatched pair, which is the same answer a
    filtered list would have given.
    """

    counselee = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label=_("Who is being billed"),
        empty_label=None,
    )
    sessions = forms.ModelMultipleChoiceField(
        queryset=SessionRecord.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label=_("Sessions to bill"),
        help_text=_("Only sessions that are not already on an invoice are listed."),
    )
    memo = forms.CharField(
        max_length=200,
        required=False,
        label=_("Note on the invoice"),
        help_text=_("The person paying will read this. “Sessions for March”."),
    )

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.case = case

        sessions = (
            SessionRecord.objects.filter(case=case)
            .uninvoiced()
            .select_related("counselee")
            .order_by("counselee__last_name", "occurred_on")
        )
        self.fields["sessions"].queryset = sessions
        self.fields["sessions"].label_from_instance = (
            lambda s: f"{s.counselee.full_name} — {s.line_description()} — {s.display_fee()}"
        )

        # Everyone who could be billed on this case: current members, plus anybody
        # with a session on it whose membership has since ended. The second half
        # matters — the last invoice on a case is usually raised after counseling
        # has finished, and a form that could not address it would leave the office
        # unable to bill the sessions it is for.
        billable_people = User.objects.filter(
            pk__in=set(
                list(case.counselees.values_list("pk", flat=True))
                + list(sessions.values_list("counselee_id", flat=True))
            )
        ).order_by("last_name", "first_name")
        self.fields["counselee"].queryset = billable_people
        self.fields["counselee"].label_from_instance = lambda user: user.full_name

    def clean(self):
        cleaned = super().clean()
        counselee = cleaned.get("counselee")
        sessions = cleaned.get("sessions") or []
        if counselee is None:
            return cleaned

        wrong = [s for s in sessions if s.counselee_id != counselee.pk]
        if wrong:
            self.add_error(
                "sessions",
                _(
                    "One invoice is addressed to one person. %(count)s of the sessions "
                    "selected are somebody else's."
                )
                % {"count": len(wrong)},
            )
        return cleaned


class InvoiceLineForm(forms.Form):
    """A charge on a draft that did not come from a session.

    A workbook, a set of materials, a retreat contribution. Not a discount: a
    negative line would make an invoice that does not foot against its own
    constraint, and the way to charge less is a lower fee or a session marked as not
    billable, both of which say why.
    """

    description = forms.CharField(
        max_length=200,
        label=_("What it is for"),
        help_text=_("The person paying will read this."),
    )
    quantity = forms.IntegerField(min_value=1, max_value=999, initial=1, label=_("How many"))
    amount = _amount_field(_("Amount each"))

    def amount_cents(self) -> int:
        return money.to_cents(self.cleaned_data["amount"])


class PaymentForm(forms.Form):
    """Money that arrived some way other than a card.

    The card route is Stripe's, and it writes its own payment row — there is no
    method choice here that would let the office claim a card payment that no
    processor saw. ``PaymentMethod.CARD`` is excluded from the choices for exactly
    that reason.
    """

    # A cent, not zero: a payment of nothing is not a payment, and the Payment model
    # refuses one anyway.
    amount = _amount_field(_("Amount received"), min_value=Decimal("0.01"))
    method = forms.ChoiceField(
        choices=[
            (value, label) for value, label in PaymentMethod.choices if value != PaymentMethod.CARD
        ],
        label=_("How it arrived"),
    )
    received_on = forms.DateField(
        initial=org_today,
        widget=forms.DateInput(attrs={"type": "date"}),
        label=_("Date received"),
    )
    reference = forms.CharField(
        max_length=120,
        required=False,
        label=_("Reference"),
        help_text=_("A check number, or how it came in. Read by the office, not the payer."),
    )

    def __init__(self, *args, invoice=None, **kwargs):
        """Defaults to the whole balance, which is what is being paid nine times out
        of ten, while leaving a part payment one edit away."""
        super().__init__(*args, **kwargs)
        if invoice is not None and not self.is_bound:
            self.fields["amount"].initial = money.from_cents(invoice.balance_cents)

    def amount_cents(self) -> int:
        return money.to_cents(self.cleaned_data["amount"])


class ReasonForm(forms.Form):
    """Why an invoice is being withdrawn or written off.

    Required, not optional, and used by both routes. An invoice that changed state
    with no explanation is the first thing an auditor asks about, and by then nobody
    remembers.
    """

    reason = forms.CharField(
        max_length=200,
        label=_("Reason"),
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_(
            "Kept on the record and in the audit trail. Nothing about the counseling belongs here."
        ),
    )


class SessionAmendForm(forms.Form):
    """Correct what a session is being charged at.

    The way out of the commonest configuration mistake — sessions recorded before
    anybody set up the fee schedule, which come out at nothing and say so on the
    billing page. Only reachable while the session is not on a live invoice.
    """

    amount = _amount_field(_("Fee for this session"))
    is_billable = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Charge for this session"),
    )
    waived_reason = forms.CharField(
        max_length=200,
        required=False,
        label=_("If not, why not"),
        help_text=_("“Waived”, “covered by the benevolence fund”."),
    )

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        if session is not None and not self.is_bound:
            self.fields["amount"].initial = money.from_cents(session.fee_cents)
            self.fields["is_billable"].initial = session.is_billable
            self.fields["waived_reason"].initial = session.waived_reason

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("is_billable") and not cleaned.get("waived_reason"):
            # The same rule as the database's ``an_unbilled_session_says_why``, said
            # here so it arrives as a sentence under the field rather than as a 500.
            self.add_error("waived_reason", _("Say why this session is not being charged for."))
        return cleaned

    def amount_cents(self) -> int:
        return money.to_cents(self.cleaned_data["amount"])
