"""
Billing views.

Every one of them resolves its object through ``for_actor`` and then re-checks a
permission, the pattern the documents app sets and the rest of the project follows.
Four things are particular to this app.

**Reading an invoice is audited.** Not because a bill is counseling content — it is
not — but because "who looked at what this counselee was charged" is a question a
ministry's accountability actually gets asked, and ``financial_admin`` can read every
invoice in the building. The same reasoning as ``CASE_VIEWED``.

**A counselee reaching the office's pages is redirected, not refused.** ``my_invoices``
sends staff to the billing index and the billing index sends a counselee to their own
invoices. The wrong door, not a locked one, which is how ``counseling:my_cases``
behaves and is right for a page whose name means different things to different roles.

**Nothing here computes money.** Every amount comes from ``services``, which is also
what the Stripe webhook and the reconciliation command call. A view that footed an
invoice itself would be a second definition of what somebody owes.

**The webhook is unauthenticated, CSRF-exempt, and answers 400 to anything it cannot
verify.** It is the one route in this application that a stranger may reach, and the
signature check in ``stripe/webhook.py`` is the whole of its authentication. It always
answers with a bare status and no body: an error message would tell whoever is probing
which part of their forgery to fix.
"""

import logging

from django.conf import settings
from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts.models import Role
from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.billing import services
from apps.billing.forms import (
    FeeForm,
    InvoiceCreateForm,
    InvoiceLineForm,
    PaymentForm,
    ReasonForm,
    SessionAmendForm,
)
from apps.billing.models import (
    Fee,
    Invoice,
    SessionRecord,
)
from apps.billing.stripe import client as stripe_client

# Aliased, because the view at the bottom of this module is called stripe_webhook and
# a plain ``import webhook as stripe_webhook`` would be shadowed by it.
from apps.billing.stripe import webhook as inbound
from apps.billing.stripe.errors import StripeError, WebhookVerificationFailed
from apps.core.dates import org_today
from apps.counseling.models import Case

logger = logging.getLogger(__name__)


def visible_case_or_404(request, public_id):
    return get_object_or_404(Case.objects.for_actor(request.user), public_id=public_id)


def visible_invoice_or_404(request, public_id):
    """The single door onto an Invoice.

    ``for_actor`` is what makes "a spouse cannot see the other's bill" true by
    construction — on a shared case each invoice is addressed to one person and only
    that person's rows are in the queryset, so this is a 404 and the invoice's
    existence is never confirmed.

    A **draft** is in a counselee's queryset and is refused by ``billing.view_invoice``
    rather than being filtered out here, which is worth stating because it is the one
    place the two layers deliberately disagree. The scoping layer answers "whose bill
    is this"; whether an amount the office is still working out may be shown to the
    person it is about is a question about state, and that belongs in ``rules.py``.
    """
    return get_object_or_404(
        Invoice.objects.for_actor(request.user).select_related(
            "case", "case__counselor", "counselee"
        ),
        public_id=public_id,
    )


def visible_session_or_404(request, public_id):
    return get_object_or_404(
        SessionRecord.objects.for_actor(request.user).select_related(
            "case", "counselee", "counselor"
        ),
        public_id=public_id,
    )


def require_perm(request, perm, obj=None):
    if not request.user.has_perm(perm, obj):
        record(
            AuditVerb.ACCESS_DENIED,
            actor=request.user,
            target=obj,
            request=request,
            permission=perm,
        )
        raise PermissionDenied
    return True


# --- the office's pages ----------------------------------------------------


@login_required
def index(request):
    """What the ministry is owed, and what is waiting to be billed.

    Three lists, in the order the work happens: sessions nobody has invoiced,
    sessions recorded with no fee because the schedule had nothing to say, and
    invoices outstanding. The middle one is the reason this page exists — without it
    an unpriced session is simply absent from every total, which is the quietest way
    for a ministry to stop charging for its counseling.
    """
    if request.user.role == Role.COUNSELEE:
        return redirect("billing:my_invoices")
    require_perm(request, "billing.view_billing_index")

    outstanding = (
        Invoice.objects.for_actor(request.user)
        .outstanding()
        .select_related("case", "counselee")
        .order_by("due_on")
    )
    return render(
        request,
        "billing/index.html",
        {
            "uninvoiced": services.uninvoiced_work(actor=request.user),
            "unpriced": services.unpriced_sessions(actor=request.user),
            "outstanding": outstanding,
            "outstanding_cents": sum(invoice.balance_cents for invoice in outstanding),
            "today": org_today(),
        },
    )


@login_required
def fees(request):
    require_perm(request, "billing.view_fees")

    return render(
        request,
        "billing/fees.html",
        {
            "fees": Fee.objects.for_actor(request.user).select_related("counselor"),
            "today": org_today(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def fee_add(request):
    require_perm(request, "billing.manage_fees")

    form = FeeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        fee = Fee(
            kind=form.cleaned_data["kind"],
            counselor=form.cleaned_data["counselor"],
            amount_cents=form.amount_cents(),
            effective_from=form.cleaned_data["effective_from"],
        )
        try:
            fee.save()
        except Exception:
            # The partial unique constraints: a rate for this kind, this counselor,
            # and this date already exists. Caught rather than pre-checked, for the
            # reason scheduling gives about read-then-write.
            logger.info("Fee refused by a constraint for kind=%s", fee.kind)
            form.add_error(None, _("There is already a rate for that from that date."))
        else:
            record(
                AuditVerb.FEE_ADDED,
                actor=request.user,
                target=fee,
                request=request,
                kind=fee.kind,
                amount_cents=fee.amount_cents,
                counselor_id=str(fee.counselor_id) if fee.counselor_id else None,
                effective_from=fee.effective_from.isoformat(),
            )
            flash.success(request, _("Rate added."))
            return redirect("billing:fees")

    return render(request, "billing/fee_form.html", {"form": form})


@login_required
@require_POST
def fee_end(request, public_id):
    """Stop a rate applying, from today.

    Ended rather than deleted: invoices were raised against it, and a rate that
    vanished would leave them unexplainable. Sessions already recorded keep the
    amount frozen into them regardless — see ``SessionRecord.fee_cents``.
    """
    require_perm(request, "billing.manage_fees")

    fee = get_object_or_404(Fee.objects.for_actor(request.user), public_id=public_id)
    fee.effective_to = org_today()
    if fee.effective_to < fee.effective_from:
        # A rate that was to start next month, withdrawn before it began. Ending it
        # on its own start date keeps the ordering constraint satisfied and means it
        # is in force for one day rather than for ever.
        fee.effective_to = fee.effective_from
    fee.save(update_fields=["effective_to", "updated_at"])

    record(
        AuditVerb.FEE_ENDED,
        actor=request.user,
        target=fee,
        request=request,
        kind=fee.kind,
        amount_cents=fee.amount_cents,
        effective_to=fee.effective_to.isoformat(),
    )
    flash.success(request, _("Rate withdrawn. Sessions already recorded keep the fee they had."))
    return redirect("billing:fees")


@login_required
@require_http_methods(["GET", "POST"])
def session_amend(request, public_id):
    session = visible_session_or_404(request, public_id)
    require_perm(request, "billing.change_session", session)

    form = SessionAmendForm(request.POST or None, session=session)
    if request.method == "POST" and form.is_valid():
        try:
            services.amend_session(
                session,
                fee_cents=form.amount_cents(),
                is_billable=form.cleaned_data["is_billable"],
                waived_reason=form.cleaned_data["waived_reason"],
                actor=request.user,
                request=request,
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            flash.success(request, _("Session updated."))
            return redirect("billing:index")

    return render(request, "billing/session_amend.html", {"form": form, "session": session})


# --- one case --------------------------------------------------------------


@login_required
def case_invoices(request, case_public_id):
    """The invoices on one case.

    Not branched on the role. A counselee sees the ones addressed to them, a
    counselor the case's, an administrator the same — the scoping layer decides, so
    this view does not have to know which.
    """
    case = visible_case_or_404(request, case_public_id)
    require_perm(request, "billing.view_case_invoices", case)

    invoices = (
        Invoice.objects.for_actor(request.user)
        # A draft is the office's working-out and is refused to the payer by
        # billing.view_invoice, so it must not be listed to them here either. See
        # InvoiceQuerySet.readable_by.
        .readable_by(request.user)
        .filter(case=case)
        .select_related("counselee")
        .order_by("-issued_on", "-id")
    )
    sessions = (
        SessionRecord.objects.for_actor(request.user)
        .filter(case=case)
        .select_related("counselee")[:50]
    )
    return render(
        request,
        "billing/case_invoices.html",
        {
            "case": case,
            "invoices": invoices,
            "sessions": sessions,
            "can_raise": request.user.has_perm("billing.add_invoice", case),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def invoice_create(request, case_public_id):
    case = visible_case_or_404(request, case_public_id)
    require_perm(request, "billing.add_invoice", case)

    form = InvoiceCreateForm(request.POST or None, case=case)
    if request.method == "POST" and form.is_valid():
        try:
            invoice = services.create_invoice(
                case=case,
                counselee=form.cleaned_data["counselee"],
                sessions=form.cleaned_data["sessions"],
                memo=form.cleaned_data["memo"],
                actor=request.user,
                request=request,
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            flash.success(
                request,
                _("Draft created. Nobody has been told about it yet — send it when it is right."),
            )
            return redirect("billing:invoice_detail", public_id=invoice.public_id)

    return render(request, "billing/invoice_create.html", {"form": form, "case": case})


# --- the payer's pages -----------------------------------------------------


@login_required
def my_invoices(request):
    """What a counselee sees: their own bills, and nothing of anyone else's."""
    if request.user.role != Role.COUNSELEE:
        return redirect("billing:index")

    invoices = (
        Invoice.objects.for_actor(request.user)
        # Drafts are the office working out what to charge. See visible_invoice_or_404
        # and InvoiceQuerySet.readable_by.
        .readable_by(request.user)
        .select_related("case")
        .order_by("-issued_on", "-id")
    )
    return render(
        request,
        "billing/my_invoices.html",
        {
            "invoices": invoices,
            "owed_cents": sum(invoice.balance_cents for invoice in invoices if invoice.is_payable),
        },
    )


@login_required
def invoice_detail(request, public_id):
    """One invoice, with its lines, its payments, and whatever can be done to it next.

    The ``can_*`` flags come from the permission layer rather than from re-reading the
    status here, so a button is shown exactly when the route behind it would allow the
    action. A page that offered "Void" on a paid invoice would be lying to the person
    clicking it.
    """
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.view_invoice", invoice)

    record(
        AuditVerb.INVOICE_VIEWED,
        actor=request.user,
        target=invoice,
        request=request,
        case_id=invoice.case_id,
        invoice=invoice.number,
    )
    return render(
        request,
        "billing/invoice_detail.html",
        {
            "invoice": invoice,
            "lines": invoice.lines.select_related("session"),
            "payments": invoice.payments.select_related("recorded_by", "reverses").order_by(
                "received_on", "id"
            ),
            "line_form": InvoiceLineForm(),
            "can_change": request.user.has_perm("billing.change_invoice", invoice),
            "can_issue": request.user.has_perm("billing.issue_invoice", invoice),
            "can_void": request.user.has_perm("billing.void_invoice", invoice),
            "can_write_off": request.user.has_perm("billing.write_off_invoice", invoice),
            "can_record_payment": request.user.has_perm("billing.record_payment", invoice),
            "can_reverse_payment": request.user.has_perm("billing.reverse_payment", invoice),
            "can_pay": request.user.has_perm("billing.pay_invoice", invoice),
            # Set by Stripe's success_url. Says "we are confirming it", not "paid":
            # the payment is only recorded when the webhook arrives, which is usually
            # a second or two behind the redirect.
            "just_paid": request.GET.get("paid") == "1",
        },
    )


@login_required
@require_POST
def pay(request, public_id):
    """Send the payer to Stripe's hosted page.

    POST, not GET, and the reason is not CSRF alone: this creates a payment session at
    Stripe, so it must not be reachable by a link somebody could be induced to click
    or by a browser prefetching.

    Nothing about the invoice changes here. The balance falls when Stripe tells us the
    money arrived, over the webhook, and never because the payer reached the return
    URL — a redirect a browser performs is not evidence of a payment.
    """
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.pay_invoice", invoice)

    # reverse() rather than a hand-built path: the route's shape is the URLconf's to
    # decide, and a literal here went stale the moment invoices stopped being keyed by
    # their primary key. SITE_BASE_URL rather than the request's host because this URL
    # is handed to Stripe, which will send a payer back to it later — see
    # apps/core/urls.py's note on why request-derived hosts are not trusted for links
    # that leave the building.
    detail_url = settings.SITE_BASE_URL + reverse(
        "billing:invoice_detail", kwargs={"public_id": invoice.public_id}
    )
    try:
        url = stripe_client.checkout_url_for(
            invoice,
            actor=request.user,
            success_url=f"{detail_url}?paid=1",
            cancel_url=detail_url,
            request=request,
        )
    except StripeError as exc:
        # Stripe's own message is logged and never shown: it is written for a
        # developer. The payer gets a sentence and an invoice in exactly the state it
        # was in before they clicked.
        logger.error("Could not start a card payment for invoice %s: %s", invoice.number, exc)
        flash.error(
            request,
            _(
                "We could not start a card payment just now. Please try again, or "
                "contact the office to pay another way."
            ),
        )
        return redirect("billing:invoice_detail", public_id=invoice.public_id)

    return redirect(url)


# --- changing one invoice --------------------------------------------------


@login_required
@require_POST
def line_add(request, public_id):
    """Put a charge on a draft that did not come from a session."""
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.change_invoice", invoice)

    form = InvoiceLineForm(request.POST)
    if not form.is_valid():
        flash.error(request, _("That charge could not be added. Check the amount."))
        return redirect("billing:invoice_detail", public_id=invoice.public_id)

    try:
        services.add_line(
            invoice,
            description=form.cleaned_data["description"],
            unit_amount_cents=form.amount_cents(),
            quantity=form.cleaned_data["quantity"],
            actor=request.user,
        )
    except services.BillingError as exc:
        flash.error(request, str(exc))
    else:
        flash.success(request, _("Added."))
    return redirect("billing:invoice_detail", public_id=invoice.public_id)


@login_required
@require_POST
def line_remove(request, public_id, line_public_id):
    """Take a charge off a draft.

    A real delete, and defensible only because a draft has not been shown to anybody
    — see ``services.remove_line``. The line is resolved through the invoice rather
    than through ``InvoiceLineItem.objects``, so a line id from another invoice is a
    404 rather than a charge removed from somebody else's bill.
    """
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.change_invoice", invoice)

    # Through the invoice's own related manager rather than
    # ``InvoiceLineItem.objects.filter(invoice=invoice)``, which is the same query and
    # a worse habit: the scoping tripwire in tests/test_role_matrix_attacks.py reads
    # ``<Model>.objects`` in views as unscoped, and it is right to, because that
    # spelling stays valid after somebody deletes the filter.
    line = get_object_or_404(invoice.lines, public_id=line_public_id)
    try:
        services.remove_line(line, actor=request.user)
    except services.BillingError as exc:
        flash.error(request, str(exc))
    else:
        flash.success(request, _("Removed."))
    return redirect("billing:invoice_detail", public_id=invoice.public_id)


@login_required
@require_POST
def invoice_issue(request, public_id):
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.issue_invoice", invoice)

    try:
        services.issue_invoice(invoice, actor=request.user, request=request)
    except services.BillingError as exc:
        flash.error(request, str(exc))
    else:
        flash.success(
            request,
            _("Sent. %(who)s has been emailed and can now see it and pay it.")
            % {"who": invoice.counselee.full_name},
        )
    return redirect("billing:invoice_detail", public_id=invoice.public_id)


@login_required
@require_http_methods(["GET", "POST"])
def invoice_void(request, public_id):
    """Withdraw an invoice, with a reason.

    Its own page rather than a button, because the reason is required and because
    voiding is the action that undoes something a counselee has already been told.
    Confirming beats a one-click mistake.
    """
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.void_invoice", invoice)

    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.void_invoice(
                invoice,
                actor=request.user,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            flash.success(
                request,
                _("Withdrawn. Its sessions can go on a new invoice."),
            )
            return redirect("billing:invoice_detail", public_id=invoice.public_id)

    return render(
        request,
        "billing/invoice_reason.html",
        {
            "form": form,
            "invoice": invoice,
            "heading": _("Withdraw this invoice"),
            "explanation": _(
                "The invoice stays on the record, marked as withdrawn, and the "
                "sessions on it become available to bill again. Use this when the "
                "invoice was wrong."
            ),
            "submit_label": _("Withdraw it"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def invoice_write_off(request, public_id):
    """Accept that an invoice will not be paid.

    Kept apart from voiding, because the two say different things about the
    ministry's books: voiding says the invoice was wrong, writing off says it was
    right. A written-off invoice can still be paid, which is why somebody settling up
    a year later is not refused.
    """
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.write_off_invoice", invoice)

    form = ReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.write_off_invoice(
                invoice,
                actor=request.user,
                reason=form.cleaned_data["reason"],
                request=request,
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            flash.success(request, _("Written off. It can still be paid if it ever is."))
            return redirect("billing:invoice_detail", public_id=invoice.public_id)

    return render(
        request,
        "billing/invoice_reason.html",
        {
            "form": form,
            "invoice": invoice,
            "heading": _("Write this invoice off"),
            "explanation": _(
                "The invoice stands and is recorded as one the ministry does not "
                "expect to collect. Nobody is emailed. It can still be paid."
            ),
            "submit_label": _("Write it off"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def payment_record(request, public_id):
    """Money that arrived by cash, check, or transfer.

    There is no card option — a card payment is Stripe's to report, and a route that
    let the office claim one would be a route that records money no processor saw.
    """
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.record_payment", invoice)

    form = PaymentForm(request.POST or None, invoice=invoice)
    if request.method == "POST" and form.is_valid():
        try:
            services.record_payment(
                invoice,
                amount_cents=form.amount_cents(),
                method=form.cleaned_data["method"],
                received_on=form.cleaned_data["received_on"],
                reference=form.cleaned_data["reference"],
                actor=request.user,
                request=request,
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            flash.success(request, _("Payment recorded."))
            return redirect("billing:invoice_detail", public_id=invoice.public_id)

    return render(request, "billing/payment_record.html", {"form": form, "invoice": invoice})


@login_required
@require_POST
def payment_reverse(request, public_id, payment_public_id):
    """A bounced check or a refunded card payment.

    Writes a second, negative row rather than editing the first; see ``Payment``.
    """
    invoice = visible_invoice_or_404(request, public_id)
    require_perm(request, "billing.reverse_payment", invoice)

    # Through the invoice, for the reason given in ``line_remove``: a payment id from
    # somebody else's bill is a 404 and not a reversal on it.
    payment = get_object_or_404(invoice.payments, public_id=payment_public_id)
    try:
        services.reverse_payment(
            payment,
            actor=request.user,
            reason=request.POST.get("reason", ""),
            request=request,
        )
    except services.BillingError as exc:
        flash.error(request, str(exc))
    else:
        flash.success(request, _("Reversed. Both entries stay on the record."))
    return redirect("billing:invoice_detail", public_id=invoice.public_id)


# --- Stripe ----------------------------------------------------------------


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe telling us something happened.

    The only unauthenticated route in this application. CSRF-exempt because Stripe
    cannot hold a token; authenticated instead by the HMAC signature over the raw
    request body, which is why ``request.body`` is passed through untouched.

    Answers are deliberately bare and deliberately coarse:

      * **400 with no body** for anything that does not verify. Every reason — no
        header, a malformed one, a wrong signature, a stale timestamp — gets the same
        answer, because a response that distinguished them would tell whoever is
        probing which part of their forgery to fix.
      * **200 for everything that verifies**, including an event we ignore and one
        whose handler failed. A 500 makes Stripe redeliver an event that will fail
        identically, every few minutes for three days; what a failed row needs is a
        person, and ``manage.py reconcile_stripe`` is what tells them.
    """
    try:
        event = inbound.verify(
            request.body,
            request.headers.get("Stripe-Signature", ""),
        )
    except WebhookVerificationFailed as exc:
        # Recorded, because a run of these is the clearest signal this application
        # produces that somebody is probing the payment endpoint. The reason is in the
        # trail and never in the response.
        logger.warning("Refused a Stripe webhook: %s", exc)
        record(
            AuditVerb.STRIPE_WEBHOOK_REFUSED,
            actor=None,
            request=request,
            reason=str(exc)[:200],
        )
        return HttpResponseBadRequest()

    inbound.handle(event, request=request)
    return HttpResponse(status=200)
