"""
What can go wrong with Stripe, sorted by what the caller should do about it.

The same taxonomy as apps/scheduling/google/errors.py, for the same reason: every
call site has to decide between "try again", "tell the person", and "this is our
bug", and a single exception class makes that decision by accident.

One difference from Google, and it changes the default. A failed calendar push is
invisible and picked up by the next cron tick. A failed payment is somebody standing
at a form with their card out — so a refusal here is shown, in words, immediately,
and the invoice is left exactly as it was.
"""


class StripeError(Exception):
    """Base. Never raised directly."""


class StripeNotConfigured(StripeError):
    """No secret key, or the integration is switched off.

    A deployment choice rather than a failure: a ministry taking cash and checks runs
    this way on purpose. The card button is not rendered, and if somebody reaches the
    route anyway they are told the ministry is not taking cards, which is true.
    """


class StripeUnavailable(StripeError):
    """A timeout, a 5xx, or a rate limit. Nothing was charged.

    Safe to retry, and safe to tell the payer to try again — Stripe's own
    idempotency keys mean a retried Checkout Session creation does not create two.
    """


class StripeRefused(StripeError):
    """Stripe understood the request and said no.

    A 400 or a 402: a malformed request, a key that has been rolled, a currency the
    account cannot take. Retrying identical input fails identically, so it is logged
    loudly with Stripe's own message and the payer is told to contact the office.
    Stripe's message is never shown to the payer — it is written for a developer and
    can quote request internals.
    """


class WebhookVerificationFailed(StripeError):
    """The request did not come from Stripe, or did not arrive in time.

    A missing header, a malformed one, a signature that does not match, or a
    timestamp outside the tolerance. All four are refused identically with a 400 and
    recorded, because the difference matters to us and must not be leaked to whoever
    is probing: a response that distinguished "bad signature" from "stale timestamp"
    would be an oracle for constructing a better attempt.
    """
