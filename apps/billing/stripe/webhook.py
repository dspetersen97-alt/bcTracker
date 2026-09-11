"""
Verifying and handling what Stripe sends back.

This is the most security-sensitive module in the application, and it is worth being
blunt about why. Every other write path in bcTracker begins with a session and a role.
This one begins with an unauthenticated HTTP POST from the internet, and what it does
is **reduce what somebody owes**. If the verification below is wrong, anybody who
finds the URL can clear a counselee's balance, and the office would have no way of
telling that from a real payment.

So four things hold, and none of them is optional.

**No secret, no processing.** With ``STRIPE_WEBHOOK_SECRET`` empty every request is
refused. Not "processed without verification" — refused. ``checks.py`` raises
``billing.E004`` at deploy time so this is found before money starts arriving rather
than after.

**The signature is checked over the raw request body.** Not the parsed JSON, not a
re-serialisation of it: ``request.body`` exactly as it arrived. Re-encoding would
change key order and whitespace, the HMAC would never match, and the temptation would
then be to loosen the check. This is why the view reads ``request.body`` and passes
bytes all the way down here.

**Timestamps are checked, so a captured delivery cannot be replayed.** The signature
alone is valid for ever. Stripe signs ``"{timestamp}.{body}"`` precisely so that a
recorded request goes stale, and the tolerance is what makes that true.

**Comparison is constant-time.** ``hmac.compare_digest``, not ``==``. A byte-by-byte
comparison that returns early leaks how much of a guess was right, which is enough to
forge a signature one byte at a time given enough attempts.

We hand-roll all of that instead of calling ``stripe.Webhook.construct_event``. That
is a real cost of the "no SDK" decision, stated in ``__init__.py`` and in the README,
and it is why the tests for this module include a wrong signature, a missing header, a
malformed header, a stale timestamp, and a body altered after signing.
"""

import hmac
import json
import logging
import time
from hashlib import sha256

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.billing import notify, services
from apps.billing.models import Invoice, StripeEvent, StripeEventOutcome
from apps.billing.stripe.errors import WebhookVerificationFailed

logger = logging.getLogger(__name__)

#: The signature scheme in Stripe's ``Stripe-Signature`` header. A ``v0`` also exists
#: for their CLI's test events and is not accepted: it is signed with a different
#: secret and would let a test tool post to a production endpoint.
SCHEME = "v1"


# --- verification ----------------------------------------------------------


def _parse_signature_header(header: str) -> tuple[int, list[str]]:
    """``t=1699999999,v1=abc,v1=def`` into a timestamp and the signatures.

    A list, not one signature: Stripe sends several while an endpoint's secret is
    being rotated, and refusing all but the first would drop every delivery during a
    key roll. Any one of them matching is a valid request.
    """
    timestamp = None
    signatures = []
    for part in (header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == SCHEME and value:
            signatures.append(value)

    if timestamp is None or not signatures:
        raise WebhookVerificationFailed("The Stripe-Signature header is missing or malformed.")
    try:
        return int(timestamp), signatures
    except ValueError as exc:
        raise WebhookVerificationFailed("The Stripe-Signature timestamp is not a number.") from exc


def _expected_signature(payload: bytes, timestamp: int) -> str:
    signed = f"{timestamp}.".encode() + payload
    return hmac.new(
        settings.STRIPE_WEBHOOK_SECRET.encode(),
        signed,
        sha256,
    ).hexdigest()


def verify(payload: bytes, signature_header: str, *, now=None) -> dict:
    """Check one delivery and return the parsed event.

    ``payload`` must be the raw bytes of the request body. Raises
    ``WebhookVerificationFailed`` for everything that is not a genuine, timely,
    Stripe-signed request — with the reason in the exception for our log and never in
    the response, so a caller probing the endpoint learns nothing from the difference.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise WebhookVerificationFailed(
            "No STRIPE_WEBHOOK_SECRET is set, so no delivery can be verified."
        )

    timestamp, signatures = _parse_signature_header(signature_header)

    now = int(now if now is not None else time.time())
    age = abs(now - timestamp)
    if age > settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS:
        # abs(), so a timestamp from the future is refused too. A clock ahead of ours
        # is a misconfiguration; a timestamp far ahead of ours is somebody buying
        # themselves a replay window.
        raise WebhookVerificationFailed(f"The delivery's timestamp is {age}s out.")

    expected = _expected_signature(payload, timestamp)
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise WebhookVerificationFailed("No signature in the header matched.")

    try:
        event = json.loads(payload.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        # Signed by Stripe's secret and yet not JSON. Should be impossible, which is
        # exactly why it is refused rather than shrugged at.
        raise WebhookVerificationFailed("The verified body was not JSON.") from exc

    if not isinstance(event, dict) or not event.get("id") or not event.get("type"):
        raise WebhookVerificationFailed("The verified body was not a Stripe event.")
    return event


# --- handling --------------------------------------------------------------


def handle(event: dict, *, request=None) -> StripeEvent:
    """Act on a verified event, exactly once.

    The ``StripeEvent`` row is claimed **before** anything is acted on, in its own
    transaction, so that a redelivery arriving while the first is still being
    processed loses the race on the unique index rather than posting the payment
    twice. That ordering is the whole point of the table.

    Never raises. A failure is recorded on the row and answered with a 200, because
    the alternative — a 500 — makes Stripe retry an event that will fail identically,
    every few minutes, for three days. What a failed row needs is a person, and
    ``manage.py reconcile_stripe`` is what tells them.
    """
    claimed = _claim(event)
    if claimed is None:
        logger.info("Stripe event %s has already been handled.", event.get("id"))
        return StripeEvent.objects.get(stripe_event_id=event["id"])

    record(
        AuditVerb.STRIPE_WEBHOOK_RECEIVED,
        actor=None,
        request=request,
        target=claimed,
        event_type=event["type"],
        stripe_event_id=event["id"],
    )

    handler = HANDLERS.get(event["type"])
    if handler is None:
        return _finish(
            claimed,
            StripeEventOutcome.IGNORED,
            f"Nothing in bcTracker acts on {event['type']}.",
        )

    try:
        outcome, detail = handler(event, request=request)
    except Exception as exc:
        logger.exception("Handling Stripe event %s failed.", event["id"])
        return _finish(claimed, StripeEventOutcome.FAILED, f"{type(exc).__name__}: {exc}"[:300])

    return _finish(claimed, outcome, detail)


def _claim(event: dict) -> StripeEvent | None:
    """Insert the row, or return None if this event has been seen before."""
    try:
        with transaction.atomic():
            return StripeEvent.objects.create(
                stripe_event_id=event["id"],
                event_type=event["type"],
            )
    except IntegrityError:
        return None


def _finish(row: StripeEvent, outcome: str, detail: str = "") -> StripeEvent:
    row.outcome = outcome
    row.detail = detail[:300]
    row.processed_at = timezone.now()
    row.save(update_fields=["outcome", "detail", "processed_at"])
    return row


def _invoice_for(obj: dict) -> Invoice | None:
    """Find the invoice a Stripe object refers to.

    By our own id in the metadata first, then by the invoice number in
    ``client_reference_id``. Two routes because a payment that cannot be matched to an
    invoice is money somebody has to chase by hand, and both are things we put there
    ourselves in ``client.create_checkout_session``.

    Read through ``Invoice.objects`` and not ``for_actor``, which is correct and worth
    saying out loud: there is no actor. Stripe is not a user of this system, the
    request carries no session, and the authorisation that matters happened when the
    signature verified.
    """
    metadata = obj.get("metadata") or {}
    invoice_id = metadata.get("invoice_id")
    if invoice_id:
        invoice = Invoice.objects.filter(pk=invoice_id).first()
        if invoice is not None:
            return invoice

    number = obj.get("client_reference_id")
    if number:
        return Invoice.objects.filter(number=number).first()
    return None


def _on_checkout_completed(event, *, request=None):
    """A payer finished a hosted Checkout page. The one event that moves money here."""
    session = (event.get("data") or {}).get("object") or {}

    if session.get("payment_status") != "paid":
        # A Checkout Session can complete without being paid — a bank debit that is
        # still processing, for instance. Recording it as a payment would tell the
        # office money had arrived when it had not.
        return (
            StripeEventOutcome.IGNORED,
            f"Checkout completed with payment_status={session.get('payment_status')!r}.",
        )

    invoice = _invoice_for(session)
    if invoice is None:
        # Loud, because this is money in Stripe's account that this ministry's books
        # do not know about. The nightly reconciliation reports it too.
        logger.error(
            "Stripe Checkout Session %s was paid but names no invoice we hold.",
            session.get("id"),
        )
        return StripeEventOutcome.FAILED, "Paid, but no matching invoice."

    amount = session.get("amount_total")
    if not isinstance(amount, int) or amount <= 0:
        return StripeEventOutcome.FAILED, f"amount_total was {amount!r}."

    payment_intent = session.get("payment_intent") or ""
    if isinstance(payment_intent, dict):
        # Stripe sends an id here, but sends the expanded object if the endpoint was
        # ever configured to expand it. Handled rather than assumed.
        payment_intent = payment_intent.get("id", "")

    payment = services.apply_stripe_payment(
        invoice,
        payment_intent_id=payment_intent,
        # **What Stripe says was taken**, not the invoice's balance. If the office
        # banked a check while the payer was on the Stripe page, the invoice is now
        # overpaid and the office needs to see that rather than have the difference
        # quietly absorbed.
        amount_cents=amount,
        reference=f"Stripe {session.get('id', '')}",
    )
    notify.payment_received(payment)
    return (
        StripeEventOutcome.PROCESSED,
        f"Recorded {amount} against {invoice.number} as payment {payment.pk}.",
    )


def _on_payment_failed(event, *, request=None):
    """A card was declined. Recorded, and nothing else.

    No email, deliberately. Stripe already tells the payer at the moment it happens,
    on the page they are looking at, and a second message from the ministry about a
    declined card is a small humiliation delivered to somebody's inbox. The invoice is
    untouched, so it stays open and the ordinary reminder covers it.
    """
    intent = (event.get("data") or {}).get("object") or {}
    invoice = _invoice_for(intent)
    where = invoice.number if invoice is not None else "an unknown invoice"
    error = (intent.get("last_payment_error") or {}).get("code", "")
    return StripeEventOutcome.PROCESSED, f"Card payment for {where} failed ({error})."


#: The events this application acts on. Everything else is recorded as ignored, which
#: is a real answer rather than a shrug: the row proves the delivery arrived and that
#: we chose not to act.
#:
#: ``invoice.paid`` is **not** here and its absence is a decision. We use Checkout
#: Sessions, not Stripe's hosted invoices, so a Stripe ``Invoice`` object never exists
#: for our bills and an ``invoice.paid`` delivery would concern something this
#: ministry did not create.
HANDLERS = {
    "checkout.session.completed": _on_checkout_completed,
    "payment_intent.payment_failed": _on_payment_failed,
}
