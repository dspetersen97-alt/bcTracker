"""
The four Stripe calls this application makes.

``ensure_customer``, ``create_checkout_session``, ``retrieve_checkout_session``,
``retrieve_payment_intent``. Everything goes through ``_request``, which is the only
place a status code is interpreted, so no caller can mistake a 402 for an outage.

**What we send Stripe is an email address, an amount, and an invoice number.** That
is the whole disclosure, and it is worth being able to say in one sentence. Not the
counselee's name, not the case, not the counselor, not the session dates, not what
the sessions were about. The line the payer sees on the Stripe page comes from
``STRIPE_PAYMENT_LABEL`` — "Counseling services" by default, deliberately as bland as
the statement descriptor, because a card statement is read by whoever opens the post.

**Nothing here is called during a test run.** ``STRIPE_ENABLED`` defaults to False and
every entry point raises ``StripeNotConfigured`` before touching the network, so the
suite cannot accidentally depend on Stripe being reachable, and a fresh checkout of
this repository does not either.
"""

import logging
import uuid

import requests
from django.conf import settings

from apps.audit.models import AuditVerb
from apps.audit.services import record, record_or_raise
from apps.billing.models import StripeCustomer
from apps.billing.stripe.errors import (
    StripeNotConfigured,
    StripeRefused,
    StripeUnavailable,
)

logger = logging.getLogger(__name__)

#: Stripe's cap on a statement descriptor, and the reason it is truncated rather
#: than validated: a ministry name too long for a card statement should still take
#: payments.
STATEMENT_DESCRIPTOR_MAX = 22


def is_configured() -> bool:
    """Whether a card payment could actually be taken.

    Read by the rules layer as well as here, so the Pay button and the route behind
    it agree. The webhook secret is not part of this check on purpose: a deployment
    that can take payments but cannot verify the callbacks is a configuration error
    for ``checks.py`` to refuse at deploy time, not something to silently downgrade
    into "no card payments" while money is arriving unrecorded.
    """
    return bool(settings.STRIPE_ENABLED and settings.STRIPE_SECRET_KEY)


def _require_configuration() -> None:
    if not is_configured():
        raise StripeNotConfigured("Stripe is not configured on this deployment.")


def _request(method: str, path: str, *, data=None, idempotency_key=None) -> dict:
    """One call to Stripe. Returns the parsed body.

    Authenticated with the secret key as HTTP Basic username and an empty password,
    which is Stripe's own scheme. Form-encoded rather than JSON, because that is what
    their API takes — nested parameters go over the wire as ``line_items[0][quantity]``
    and ``requests`` sends the flat dict as-is.
    """
    _require_configuration()

    headers = {}
    if idempotency_key:
        # Stripe deduplicates on this for 24 hours, which is what makes a retried
        # POST safe. Only sent for writes; a GET needs no key.
        headers["Idempotency-Key"] = idempotency_key

    try:
        response = requests.request(
            method,
            f"{settings.STRIPE_API_BASE}{path}",
            data=data,
            headers=headers,
            auth=(settings.STRIPE_SECRET_KEY, ""),
            timeout=settings.STRIPE_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise StripeUnavailable(f"Could not reach Stripe: {exc}") from exc

    return _interpret(response, method, path)


def _interpret(response, method, path) -> dict:
    status = response.status_code

    if status == 429 or status >= 500:
        raise StripeUnavailable(f"Stripe returned {status} for {method} {path}.")

    if status >= 400:
        detail = _error_detail(response)
        # Logged in full and never shown: Stripe's messages are written for a
        # developer and quote request internals.
        logger.error("Stripe refused %s %s: %s", method, path, detail)
        raise StripeRefused(f"Stripe returned {status}: {detail}")

    try:
        body = response.json()
    except ValueError as exc:
        raise StripeUnavailable("Stripe's response was not JSON.") from exc
    return body if isinstance(body, dict) else {}


def _error_detail(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    error = body.get("error")
    if isinstance(error, dict):
        parts = [error.get("type", ""), error.get("code", ""), error.get("message", "")]
        return " ".join(part for part in parts if part)[:300]
    return str(error)[:300]


# --- customers -------------------------------------------------------------


def ensure_customer(counselee, *, actor=None, request=None) -> StripeCustomer:
    """The counselee's Stripe customer record, created on first use.

    A customer rather than a bare charge because it is what lets somebody pay a
    second invoice without retyping a card, and because Stripe's receipts go to the
    address on it.

    Recorded with ``record_or_raise``, the same as a document download and for the
    same reason: this is the moment a counselee's email address leaves the building.
    If the trail cannot be written, the address is not sent.
    """
    _require_configuration()

    existing = StripeCustomer.objects.filter(counselee=counselee).first()
    if existing is not None:
        return existing

    record_or_raise(
        AuditVerb.STRIPE_CUSTOMER_CREATED,
        actor=actor,
        target=counselee,
        request=request,
        counselee_id=str(counselee.pk),
        # The fact of the disclosure and what was disclosed. The address itself is
        # already in the accounts table; what is new is that Stripe now has it.
        disclosed="email address",
    )

    payload = _request(
        "POST",
        "/customers",
        data={
            "email": counselee.email,
            # No name, no phone, no address. Stripe asks for a name at the card form
            # if the account requires one, which keeps it between the payer and
            # Stripe rather than passing through us.
            "metadata[bctracker_user_id]": str(counselee.pk),
        },
        # One customer per counselee even if two requests race — the retry of a
        # timed-out POST must not create a second record at Stripe.
        idempotency_key=f"bctracker-customer-{counselee.pk}",
    )

    return StripeCustomer.objects.create(
        counselee=counselee,
        stripe_customer_id=payload["id"],
        email=counselee.email,
    )


# --- checkout --------------------------------------------------------------


def create_checkout_session(invoice, *, customer, success_url, cancel_url) -> dict:
    """A one-shot hosted payment page for this invoice's balance.

    The amount is the **balance**, not the total: somebody who paid half in cash pays
    the rest here. It is computed at this moment and sent to Stripe, which is why
    ``webhook.py`` records what Stripe says arrived rather than assuming the invoice
    was cleared — a session created yesterday for $340 must not be treated as
    settling a bill that has since had $100 paid against it.
    """
    _require_configuration()

    amount = invoice.balance_cents
    if amount <= 0:
        raise StripeRefused("There is nothing outstanding on that invoice.")

    data = {
        "mode": "payment",
        "customer": customer.stripe_customer_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
        # Both of these come back on the webhook, and either is enough to find the
        # invoice. Two of them because a payment that cannot be matched to an invoice
        # is money the office has to reconcile by hand.
        "client_reference_id": invoice.number,
        "metadata[invoice_id]": str(invoice.pk),
        "metadata[invoice_number]": invoice.number,
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": invoice.currency,
        "line_items[0][price_data][unit_amount]": str(amount),
        # One line, saying as little as the statement descriptor does. The itemised
        # detail is on the invoice in this application, where the access rules are.
        "line_items[0][price_data][product_data][name]": settings.STRIPE_PAYMENT_LABEL,
        "payment_intent_data[description]": f"Invoice {invoice.number}",
        "payment_intent_data[metadata][invoice_id]": str(invoice.pk),
        # Stripe emails its own receipt here, which is why the address is worth
        # keeping current on the customer record.
        "customer_update[address]": "auto",
    }
    if settings.STRIPE_STATEMENT_DESCRIPTOR:
        data["payment_intent_data[statement_descriptor]"] = settings.STRIPE_STATEMENT_DESCRIPTOR[
            :STATEMENT_DESCRIPTOR_MAX
        ]

    return _request(
        "POST",
        "/checkout/sessions",
        data=data,
        # A fresh key per attempt. Deliberately *not* keyed on the invoice: within
        # Stripe's 24-hour window that would replay the first session, and sending a
        # payer to an expired Checkout page is worse than creating a second one.
        # Creating two is harmless, because reuse is handled below by asking Stripe
        # whether the previous session is still open.
        idempotency_key=f"bctracker-checkout-{invoice.pk}-{uuid.uuid4()}",
    )


def retrieve_checkout_session(session_id: str) -> dict:
    return _request("GET", f"/checkout/sessions/{session_id}")


def retrieve_payment_intent(payment_intent_id: str) -> dict:
    return _request("GET", f"/payment_intents/{payment_intent_id}")


def checkout_url_for(invoice, *, actor, success_url, cancel_url, request=None) -> str:
    """Where to send the payer. The only function the view needs.

    Reuses the invoice's existing Checkout Session while Stripe still says it is
    open, which is what stops a payer who clicked Pay, thought better of it, and came
    back an hour later from accumulating live payment pages for the same bill. A
    session Stripe has expired, or one it no longer recognises, is replaced.
    """
    customer = ensure_customer(invoice.counselee, actor=actor, request=request)

    if invoice.stripe_checkout_session_id:
        reusable = _reusable_session(invoice.stripe_checkout_session_id)
        if reusable:
            return reusable

    session = create_checkout_session(
        invoice,
        customer=customer,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    invoice.stripe_customer_id = customer.stripe_customer_id
    invoice.stripe_checkout_session_id = session["id"]
    invoice.save(update_fields=["stripe_customer_id", "stripe_checkout_session_id", "updated_at"])
    record(
        AuditVerb.STRIPE_CHECKOUT_STARTED,
        actor=actor,
        target=invoice,
        request=request,
        case_id=invoice.case_id,
        invoice=invoice.number,
        amount_cents=invoice.balance_cents,
        checkout_session=session["id"],
    )
    return session["url"]


def _reusable_session(session_id: str) -> str:
    """The URL of an existing session if it can still be paid, else "".

    Never raises. A Stripe failure here has to fall through to creating a new
    session: refusing to let somebody pay because we could not check on the page they
    abandoned would be a poor trade.
    """
    try:
        session = retrieve_checkout_session(session_id)
    except Exception:
        logger.info("Could not re-read Checkout Session %s; creating a new one.", session_id)
        return ""

    if session.get("status") == "open" and session.get("url"):
        return session["url"]
    return ""
