"""
The Stripe integration: two modules, one outward and one inward.

``client.py`` makes the four calls this application needs. ``webhook.py`` verifies
and handles what Stripe sends back.

**Written against the REST API with `requests`, not the `stripe` SDK.** A departure
from the approved plan, taken for the same reason the Google integration does not use
`google-api-python-client`: the SDK is a large dependency, updated often, that would
sit in the path between a counselee's card and this ministry's records to save
perhaps eighty lines. Four form-encoded POSTs and one HMAC comparison are things this
project can own, test offline, and read in full. The cost is stated plainly — we
hand-roll the signature check that ``stripe.Webhook.construct_event`` would do for
us, so ``webhook.py`` carries the reasoning in unusual detail and its tests include
the attacks the SDK's version is there to stop.

**Hosted Checkout, not Stripe's hosted invoices.** Also a departure. Stripe invoices
would mean mirroring every line into Stripe, which means a counselee's session dates
and the ministry's fee schedule living in a second system, and two records of what is
owed that can disagree. A Checkout Session is a one-shot "take this amount against
this reference": Stripe learns an email address, an amount, and an invoice number,
and this application stays authoritative. Both departures are recorded in the README.

**Card data never touches this application**, which is the whole point of both
choices. The payer is sent to a page Stripe serves and comes back with nothing but a
session id.
"""
