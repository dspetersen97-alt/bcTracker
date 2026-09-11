"""
The one unauthenticated write path in this application.

Every other route in bcTracker starts with a session and a role. This one starts
with an HTTP POST from the internet, and what it does is reduce what somebody owes.
If the verification is wrong, anybody who finds the URL can clear a counselee's
balance and the office cannot tell that from a real payment — so this file is
written as an attempt on it rather than as a happy path with edge cases.

Three groups, in the order an attacker would meet them:

  * **Verification.** No secret, no header, a malformed header, a ``v0`` scheme, a
    stale or future timestamp, a wrong signature, and a body altered after signing.
    Every one of them gets the same bare 400, which is asserted too: a response that
    distinguished them would say which part of the forgery to fix.
  * **Exactly once.** A redelivery of an event already handled must not post the
    payment again. Stripe retries as a matter of course, so this is the ordinary case
    and not the exceptional one.
  * **What the handlers do and refuse to do.** A Checkout Session that completed
    without being paid, one naming an invoice we do not hold, an amount that is not a
    positive integer, and a declined card — which is recorded and emails nobody.

The signatures are built here rather than mocked out. A test that stubbed
``verify`` would pass with the HMAC removed entirely.
"""

import hmac
import json
import time
from datetime import timedelta
from hashlib import sha256

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.billing import services
from apps.billing.models import (
    Fee,
    FeeKind,
    InvoiceStatus,
    Payment,
    StripeEvent,
    StripeEventOutcome,
)
from apps.billing.stripe import webhook as inbound
from apps.billing.stripe.errors import WebhookVerificationFailed
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember

pytestmark = pytest.mark.django_db

SECRET = "whsec_a_test_secret"  # noqa: S105 — a test secret, and it is meant to look like one


@pytest.fixture(autouse=True)
def stripe_configured(settings):
    """Stripe on, with a webhook secret, for every test in this file.

    ``STRIPE_ENABLED`` is off in the test settings so no other test can reach out to
    the network by accident; the webhook does not consult it, but the payment path
    behind it reads more honestly with it on.
    """
    settings.STRIPE_ENABLED = True
    settings.STRIPE_SECRET_KEY = "sk_test_not_real"  # noqa: S105
    settings.STRIPE_WEBHOOK_SECRET = SECRET
    settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS = 300
    return settings


@pytest.fixture
def invoice(counselor, counselee, make_user):
    """One open invoice for $85.00, raised the way the office would raise it."""
    case = Case.objects.create(counselor=counselor, label="Ashford — individual")
    CaseMember.objects.create(case=case, counselee=counselee)
    Fee.objects.create(
        kind=FeeKind.SESSION,
        amount_cents=8500,
        effective_from=org_today() - timedelta(days=30),
    )
    biller = make_user(Role.FINANCIAL_ADMIN)
    session = services.record_session(case=case, counselee=counselee, actor=biller)
    invoice = services.issue_invoice(
        services.create_invoice(case=case, counselee=counselee, sessions=[session], actor=biller),
        actor=biller,
    )
    mail.outbox.clear()
    return invoice


# --- building a delivery ---------------------------------------------------


def sign(payload: bytes, *, timestamp=None, secret=SECRET, scheme="v1") -> str:
    """The ``Stripe-Signature`` header Stripe would have sent for this body."""
    timestamp = int(timestamp if timestamp is not None else time.time())
    signed = f"{timestamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, sha256).hexdigest()
    return f"t={timestamp},{scheme}={digest}"


def body_for(event: dict) -> bytes:
    """The raw bytes, kept as the bytes. Re-serialising would change key order and
    whitespace, and the signature is over what arrived — see the module docstring in
    apps/billing/stripe/webhook.py."""
    return json.dumps(event).encode()


def checkout_event(invoice=None, *, event_id="evt_1", amount=8500, status="paid", **extra):
    session = {
        "id": "cs_test_1",
        "object": "checkout.session",
        "payment_status": status,
        "amount_total": amount,
        "payment_intent": "pi_test_1",
        "metadata": {"invoice_id": str(invoice.pk)} if invoice is not None else {},
        "client_reference_id": invoice.number if invoice is not None else None,
    }
    session.update(extra)
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": session},
    }


def post(client, payload: bytes, header: str):
    return client.post(
        reverse("billing:stripe_webhook"),
        data=payload,
        content_type="application/json",
        headers={"stripe-signature": header},
    )


def deliver(client, event: dict, **sign_kwargs):
    payload = body_for(event)
    return post(client, payload, sign(payload, **sign_kwargs))


# --- verification ----------------------------------------------------------


class TestVerification:
    def test_a_genuine_delivery_verifies(self, invoice):
        payload = body_for(checkout_event(invoice))

        event = inbound.verify(payload, sign(payload))

        assert event["id"] == "evt_1"

    def test_with_no_secret_configured_nothing_is_accepted(self, settings, invoice):
        """Refused, not "processed without verification". checks.py raises
        billing.E004 at deploy time so this is found before money arrives."""
        settings.STRIPE_WEBHOOK_SECRET = ""
        payload = body_for(checkout_event(invoice))

        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(payload, sign(payload, secret=""))

    def test_a_missing_header_is_refused(self, invoice):
        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(body_for(checkout_event(invoice)), "")

    @pytest.mark.parametrize(
        "header",
        [
            "nonsense",
            "t=1699999999",  # a timestamp and no signature
            "v1=deadbeef",  # a signature and no timestamp
            "t=,v1=deadbeef",
            "t=not-a-number,v1=deadbeef",
            "t=1699999999,v1=",
        ],
    )
    def test_a_malformed_header_is_refused(self, invoice, header):
        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(body_for(checkout_event(invoice)), header)

    def test_the_v0_scheme_is_not_accepted(self, invoice):
        """v0 is Stripe's CLI test scheme, signed with a different secret. Accepting
        it would let a test tool post to a production endpoint."""
        payload = body_for(checkout_event(invoice))

        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(payload, sign(payload, scheme="v0"))

    def test_a_stale_delivery_is_refused(self, invoice):
        """The signature alone is valid for ever. The timestamp is what makes a
        captured request go stale."""
        payload = body_for(checkout_event(invoice))
        old = time.time() - 3600

        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(payload, sign(payload, timestamp=old))

    def test_a_delivery_from_the_future_is_refused_too(self, invoice):
        """A clock ahead of ours is a misconfiguration; a timestamp far ahead of ours
        is somebody buying themselves a replay window."""
        payload = body_for(checkout_event(invoice))
        ahead = time.time() + 3600

        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(payload, sign(payload, timestamp=ahead))

    def test_one_just_inside_the_tolerance_is_accepted(self, invoice, settings):
        settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS = 300
        payload = body_for(checkout_event(invoice))

        event = inbound.verify(payload, sign(payload, timestamp=time.time() - 290))

        assert event["type"] == "checkout.session.completed"

    def test_a_signature_made_with_another_secret_is_refused(self, invoice):
        payload = body_for(checkout_event(invoice))

        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(payload, sign(payload, secret="whsec_someone_elses"))

    def test_a_body_altered_after_signing_is_refused(self, invoice):
        """The attack the HMAC exists for: a real captured delivery with the amount
        raised, or the invoice id swapped for somebody else's."""
        payload = body_for(checkout_event(invoice))
        header = sign(payload)
        tampered = payload.replace(b'"amount_total": 8500', b'"amount_total": 1')

        assert tampered != payload
        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(tampered, header)

    def test_any_one_of_several_signatures_matching_is_enough(self, invoice):
        """Stripe sends more than one while an endpoint's secret is being rotated.
        Refusing all but the first would drop every delivery during a key roll."""
        payload = body_for(checkout_event(invoice))
        timestamp = int(time.time())
        good = sign(payload, timestamp=timestamp).split("v1=")[1]
        header = f"t={timestamp},v1=deadbeef,v1={good}"

        assert inbound.verify(payload, header)["id"] == "evt_1"

    def test_a_verified_body_that_is_not_json_is_refused(self):
        """Signed with our own secret and still not JSON. Should be impossible, which
        is exactly why it is refused rather than shrugged at."""
        payload = b"this is not json"

        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(payload, sign(payload))

    @pytest.mark.parametrize("verified", [b"[]", b'"a string"', b"{}", b'{"id": "evt_1"}'])
    def test_a_verified_body_that_is_not_an_event_is_refused(self, verified):
        with pytest.raises(WebhookVerificationFailed):
            inbound.verify(verified, sign(verified))


class TestTheRouteItself:
    def test_a_genuine_delivery_is_answered_200(self, client, invoice):
        assert deliver(client, checkout_event(invoice)).status_code == 200

    @pytest.mark.parametrize(
        ("payload", "header"),
        [
            (b'{"id": "evt_1", "type": "checkout.session.completed"}', ""),
            (b'{"id": "evt_1", "type": "checkout.session.completed"}', "t=1,v1=deadbeef"),
            (b"", "nonsense"),
        ],
    )
    def test_anything_that_does_not_verify_is_a_bare_400(self, client, payload, header):
        """The same answer for every reason, with nothing in the body. A response that
        distinguished a stale timestamp from a wrong signature would tell whoever is
        probing which part of their forgery to fix."""
        response = post(client, payload, header)

        assert response.status_code == 400
        assert response.content == b""

    def test_a_refusal_is_audited(self, client):
        post(client, b"{}", "nonsense")

        event = AuditEvent.objects.filter(verb=AuditVerb.STRIPE_WEBHOOK_REFUSED).first()
        assert event is not None
        assert event.actor is None

    def test_the_reason_is_in_the_trail_and_not_in_the_response(self, client):
        response = post(client, b"{}", "nonsense")

        event = AuditEvent.objects.filter(verb=AuditVerb.STRIPE_WEBHOOK_REFUSED).first()
        assert event.metadata["reason"]
        assert event.metadata["reason"].encode() not in response.content

    def test_an_accepted_delivery_is_audited_with_no_actor(self, client, invoice):
        deliver(client, checkout_event(invoice))

        event = AuditEvent.objects.filter(verb=AuditVerb.STRIPE_WEBHOOK_RECEIVED).first()
        assert event.actor is None
        assert event.metadata["event_type"] == "checkout.session.completed"

    def test_it_takes_nothing_but_a_post(self, client):
        assert client.get(reverse("billing:stripe_webhook")).status_code == 405

    def test_it_needs_no_session_and_no_csrf_token(self, client, invoice):
        """Asserted because both are exemptions, and an exemption nobody tests is an
        exemption somebody removes and then restores under pressure."""
        assert deliver(client, checkout_event(invoice)).status_code == 200
        assert "_auth_user_id" not in client.session


# --- exactly once ----------------------------------------------------------


class TestARedelivery:
    """Stripe retries an event it did not get a prompt answer to. This is the
    ordinary case, not the exceptional one."""

    def test_the_money_is_only_recorded_once(self, client, invoice):
        event = checkout_event(invoice)

        assert deliver(client, event).status_code == 200
        assert deliver(client, event).status_code == 200

        invoice.refresh_from_db()
        assert invoice.payments.count() == 1
        assert invoice.amount_paid_cents == 8500

    def test_only_one_row_is_kept_for_it(self, client, invoice):
        event = checkout_event(invoice)

        deliver(client, event)
        deliver(client, event)

        assert StripeEvent.objects.filter(stripe_event_id="evt_1").count() == 1

    def test_and_the_second_delivery_does_not_email_a_second_receipt(self, client, invoice):
        event = checkout_event(invoice)

        deliver(client, event)
        deliver(client, event)

        assert len(mail.outbox) == 1

    def test_a_row_claimed_but_never_finished_still_blocks_a_retry(self, client, invoice):
        """The claim is written **before** the handler runs, in its own transaction, so
        a redelivery arriving while the first is still being processed loses the race
        on the unique index rather than posting the payment twice."""
        StripeEvent.objects.create(stripe_event_id="evt_1", event_type="checkout.session.completed")

        assert deliver(client, checkout_event(invoice)).status_code == 200

        assert Payment.objects.count() == 0

    def test_two_different_events_for_the_same_charge_still_pay_once(self, client, invoice):
        """The second guard, independent of the first: two distinct event ids naming
        the same PaymentIntent. The StripeEvent table cannot help here, and the
        payment must still land once."""
        deliver(client, checkout_event(invoice, event_id="evt_1"))
        deliver(client, checkout_event(invoice, event_id="evt_2"))

        invoice.refresh_from_db()
        assert invoice.payments.count() == 1
        assert invoice.amount_paid_cents == 8500


# --- what the handlers do --------------------------------------------------


class TestAPaidCheckoutSession:
    def test_the_payment_is_recorded_against_the_invoice(self, client, invoice):
        deliver(client, checkout_event(invoice))

        invoice.refresh_from_db()
        payment = invoice.payments.get()
        assert (invoice.status, invoice.balance_cents) == (InvoiceStatus.PAID, 0)
        assert payment.stripe_payment_intent_id == "pi_test_1"
        assert payment.recorded_by_id is None

    def test_the_event_is_recorded_as_processed(self, client, invoice):
        deliver(client, checkout_event(invoice))

        row = StripeEvent.objects.get(stripe_event_id="evt_1")
        assert row.outcome == StripeEventOutcome.PROCESSED
        assert row.processed_at is not None

    def test_the_payer_gets_a_receipt(self, client, invoice):
        """Only for a card payment, and only because they were not in the room — they
        typed their card into Stripe's page and came back to ours."""
        deliver(client, checkout_event(invoice))

        assert len(mail.outbox) == 1
        assert invoice.number in mail.outbox[0].body

    def test_it_can_be_matched_by_the_invoice_number_alone(self, client, invoice):
        """The second of the two routes in ``_invoice_for``. A payment that cannot be
        matched is money somebody has to chase by hand, so there are two."""
        event = checkout_event(invoice)
        event["data"]["object"]["metadata"] = {}

        deliver(client, event)

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID

    def test_an_expanded_payment_intent_is_handled_rather_than_assumed(self, client, invoice):
        event = checkout_event(invoice, payment_intent={"id": "pi_expanded", "object": "x"})

        deliver(client, event)

        assert invoice.payments.get().stripe_payment_intent_id == "pi_expanded"

    def test_what_stripe_says_was_taken_is_what_is_recorded(self, client, invoice):
        """Not the invoice's balance. If the office banked a check while the payer was
        on the Stripe page the invoice is now overpaid, and the office needs to see
        that rather than have the difference quietly absorbed."""
        event = checkout_event(invoice, amount=9000)

        deliver(client, event)

        invoice.refresh_from_db()
        assert (invoice.amount_paid_cents, invoice.balance_cents) == (9000, -500)

    def test_one_that_completed_without_being_paid_records_no_money(self, client, invoice):
        """A bank debit still processing, for instance. Recording it would tell the
        office money had arrived when it had not."""
        deliver(client, checkout_event(invoice, status="unpaid"))

        invoice.refresh_from_db()
        row = StripeEvent.objects.get(stripe_event_id="evt_1")
        assert invoice.payments.count() == 0
        assert row.outcome == StripeEventOutcome.IGNORED
        assert "unpaid" in row.detail

    def test_one_naming_no_invoice_we_hold_is_recorded_as_a_failure(self, client, invoice):
        """Money in Stripe's account that this ministry's books do not know about.
        Answered 200 — retrying will not make the invoice appear — and left for the
        nightly reconciliation to report."""
        event = checkout_event(invoice)
        event["data"]["object"]["metadata"] = {"invoice_id": "999999"}
        event["data"]["object"]["client_reference_id"] = "BC-999999"

        response = deliver(client, event)

        assert response.status_code == 200
        assert StripeEvent.objects.get(stripe_event_id="evt_1").outcome == (
            StripeEventOutcome.FAILED
        )
        assert Payment.objects.count() == 0

    @pytest.mark.parametrize("amount", [0, -100, None, "8500", 85.0])
    def test_an_amount_that_is_not_a_positive_whole_number_is_refused(
        self, client, invoice, amount
    ):
        """Stripe is denominated in the smallest unit, like this application, so a
        string or a float here means something is wrong rather than something that
        needs converting."""
        deliver(client, checkout_event(invoice, amount=amount))

        assert Payment.objects.count() == 0
        assert StripeEvent.objects.get(stripe_event_id="evt_1").outcome == (
            StripeEventOutcome.FAILED
        )

    def test_the_invoice_is_not_altered_when_the_handler_fails(self, client, invoice, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("Stripe's shape changed under us")

        monkeypatch.setitem(inbound.HANDLERS, "checkout.session.completed", explode)

        response = deliver(client, checkout_event(invoice))

        invoice.refresh_from_db()
        assert response.status_code == 200
        assert invoice.status == InvoiceStatus.OPEN
        row = StripeEvent.objects.get(stripe_event_id="evt_1")
        assert row.outcome == StripeEventOutcome.FAILED
        assert "RuntimeError" in row.detail

    def test_a_500_is_never_the_answer(self, client, invoice, monkeypatch):
        """A 500 makes Stripe redeliver an event that will fail identically, every few
        minutes for three days. What a failed row needs is a person."""

        def explode(*args, **kwargs):
            raise RuntimeError("still broken")

        monkeypatch.setitem(inbound.HANDLERS, "checkout.session.completed", explode)

        assert deliver(client, checkout_event(invoice)).status_code == 200


class TestADeclinedCard:
    @pytest.fixture
    def declined(self, invoice):
        return {
            "id": "evt_declined",
            "type": "payment_intent.payment_failed",
            "data": {
                "object": {
                    "id": "pi_declined",
                    "metadata": {"invoice_id": str(invoice.pk)},
                    "last_payment_error": {"code": "card_declined"},
                }
            },
        }

    def test_it_is_recorded(self, client, invoice, declined):
        deliver(client, declined)

        row = StripeEvent.objects.get(stripe_event_id="evt_declined")
        assert row.outcome == StripeEventOutcome.PROCESSED
        assert "card_declined" in row.detail

    def test_the_invoice_is_untouched_and_stays_open(self, client, invoice, declined):
        deliver(client, declined)

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.OPEN
        assert invoice.payments.count() == 0

    def test_nobody_is_emailed(self, client, invoice, declined):
        """Deliberate. Stripe already told the payer at the moment it happened, on the
        page they were looking at; a second message from the ministry about a declined
        card is a small humiliation delivered to somebody's inbox."""
        deliver(client, declined)

        assert mail.outbox == []


class TestEverythingElseStripeSends:
    def test_an_event_we_do_not_act_on_is_recorded_as_ignored(self, client):
        """A real answer rather than a shrug: the row proves the delivery arrived and
        that we chose not to act."""
        event = {"id": "evt_other", "type": "customer.updated", "data": {"object": {}}}

        response = deliver(client, event)

        row = StripeEvent.objects.get(stripe_event_id="evt_other")
        assert response.status_code == 200
        assert row.outcome == StripeEventOutcome.IGNORED
        assert "customer.updated" in row.detail

    def test_invoice_paid_is_one_of_them(self, client, invoice):
        """Its absence from HANDLERS is a decision. We use Checkout Sessions, not
        Stripe's hosted invoices, so a Stripe Invoice object never exists for our bills
        and an invoice.paid delivery concerns something this ministry did not create.
        """
        event = {"id": "evt_inv", "type": "invoice.paid", "data": {"object": {}}}

        deliver(client, event)

        invoice.refresh_from_db()
        assert StripeEvent.objects.get(stripe_event_id="evt_inv").outcome == (
            StripeEventOutcome.IGNORED
        )
        assert invoice.status == InvoiceStatus.OPEN


class TestWhatIsNotKept:
    def test_the_payload_is_not_stored(self, client, invoice):
        """It would be the one copy of a counselee's billing data in this database that
        no access rule governs. The event id, the type, and what we did with it answer
        every question this table exists for.
        """
        deliver(client, checkout_event(invoice))

        fields = {f.name for f in StripeEvent._meta.get_fields()}
        assert {"payload", "body", "data"} & fields == set()
        # And nowhere else, either: what is kept is one line for a person reading the
        # list, not the delivery with a different name on it.
        row = StripeEvent.objects.get(stripe_event_id="evt_1")
        assert body_for(checkout_event(invoice)).decode() not in row.detail
        assert "\n" not in row.detail

    def test_the_detail_line_carries_no_counseling_content(self, client, invoice):
        deliver(client, checkout_event(invoice))

        detail = StripeEvent.objects.get(stripe_event_id="evt_1").detail
        assert invoice.case.label not in detail
        assert invoice.counselee.email not in detail


# --- refusing a half-configured deployment --------------------------------


class TestTheDeployChecks:
    """The checks that make a misconfiguration a startup failure rather than a
    discovery from the log — the same shape as the calendar's, and one of them is the
    most important check in the project.

    A webhook endpoint with no signing secret is an unauthenticated "mark this invoice
    paid" URL open to the internet. ``webhook.py`` refuses every delivery in that
    state, which is the real protection; this is what stops a container reaching it.
    """

    def _run(self):
        from django.core.checks import run_checks

        return {problem.id for problem in run_checks(include_deployment_checks=True)}

    def test_stripe_left_off_raises_nothing(self, settings):
        """A ministry taking cash and checks. Not a half-configured deployment."""
        settings.STRIPE_ENABLED = False
        settings.STRIPE_SECRET_KEY = ""
        settings.STRIPE_WEBHOOK_SECRET = ""

        assert not {"billing.E003", "billing.E004", "billing.E005", "billing.W001"} & self._run()

    def test_switching_it_on_without_a_secret_key_is_refused(self, settings):
        settings.STRIPE_SECRET_KEY = ""

        assert "billing.E003" in self._run()

    def test_switching_it_on_without_a_webhook_secret_is_refused(self, settings):
        settings.STRIPE_WEBHOOK_SECRET = ""

        assert "billing.E004" in self._run()

    def test_a_test_key_in_production_is_a_warning(self, settings):
        """Cards appear to be charged and no money moves. Not fatal — a staging
        deployment is meant to run this way — so it warns rather than refuses."""
        assert "billing.W001" in self._run()

    def test_a_development_site_url_is_refused(self, settings):
        """Stripe sends the payer back to this address after taking their card, so
        localhost here fails after the charge, with nothing to tell them whether it
        worked."""
        settings.SITE_BASE_URL = "https://localhost"

        assert "billing.E005" in self._run()

    def test_an_absurd_tolerance_is_a_warning(self, settings):
        settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS = 86400

        assert "billing.W002" in self._run()

    def test_an_invoice_due_date_in_the_past_is_refused(self, settings):
        """Every invoice would be issued already overdue, and the reminder job would
        chase a bill nobody has had a chance to pay."""
        settings.BILLING_DUE_DAYS = -1

        assert "billing.E001" in self._run()

    def test_a_currency_stripe_cannot_read_is_refused(self, settings):
        settings.BILLING_CURRENCY = "dollars"

        assert "billing.E002" in self._run()

    def test_a_fully_configured_deployment_passes(self, settings):
        settings.STRIPE_SECRET_KEY = "sk_live_not_real"  # noqa: S105
        settings.SITE_BASE_URL = "https://counseling.example.org"

        assert (
            not {
                "billing.E001",
                "billing.E002",
                "billing.E003",
                "billing.E004",
                "billing.E005",
                "billing.W001",
                "billing.W002",
            }
            & self._run()
        )
