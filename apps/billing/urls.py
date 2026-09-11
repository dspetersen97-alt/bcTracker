"""
Billing routes.

An invoice is addressed by its own primary key with no case in the path, the same as
a document and a thread: the scoping layer decides reachability, so a case id in the
URL would be decoration that invites a view to trust it. The two routes nested under
a case are the ones whose subject is the case — its list of invoices, and raising a
new one, which needs a case before there is an invoice to name.

``invoices/`` is the counselee's own list and sits at the top level rather than under
``billing/``, because "billing" is the office's word for it. Somebody paying looks for
their invoices.

**There is no route that deletes anything.** No invoice delete, no payment delete, no
session delete. An invoice is voided, a payment is reversed by its opposite, and a
session that should not be charged for is marked as such with a reason. Their absence
is the policy; see the notes in ``rules.py`` and ``models.py``.

**The webhook is the only unauthenticated route in this application.** It is exempt
from CSRF because Stripe cannot hold a token, and its authentication is the HMAC
signature over the raw body instead — which is why ``apps/billing/stripe/webhook.py``
carries the reasoning it does.
"""

from django.urls import path

from apps.billing import views

app_name = "billing"

urlpatterns = [
    # --- the office's pages ------------------------------------------------
    path("billing/", views.index, name="index"),
    path("billing/fees/", views.fees, name="fees"),
    path("billing/fees/new/", views.fee_add, name="fee_add"),
    path("billing/fees/<publicid:public_id>/end/", views.fee_end, name="fee_end"),
    path("billing/sessions/<publicid:public_id>/", views.session_amend, name="session_amend"),
    # --- one case ----------------------------------------------------------
    path("cases/<publicid:case_public_id>/invoices/", views.case_invoices, name="case_invoices"),
    path(
        "cases/<publicid:case_public_id>/invoices/new/", views.invoice_create, name="invoice_create"
    ),
    # --- the payer's pages -------------------------------------------------
    path("invoices/", views.my_invoices, name="my_invoices"),
    path("invoices/<publicid:public_id>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<publicid:public_id>/pay/", views.pay, name="pay"),
    # --- changing one invoice ---------------------------------------------
    path("invoices/<publicid:public_id>/lines/new/", views.line_add, name="line_add"),
    path(
        "invoices/<publicid:public_id>/lines/<publicid:line_public_id>/remove/",
        views.line_remove,
        name="line_remove",
    ),
    path("invoices/<publicid:public_id>/issue/", views.invoice_issue, name="invoice_issue"),
    path("invoices/<publicid:public_id>/void/", views.invoice_void, name="invoice_void"),
    path(
        "invoices/<publicid:public_id>/write-off/",
        views.invoice_write_off,
        name="invoice_write_off",
    ),
    path(
        "invoices/<publicid:public_id>/payments/new/", views.payment_record, name="payment_record"
    ),
    path(
        "invoices/<publicid:public_id>/payments/<publicid:payment_public_id>/reverse/",
        views.payment_reverse,
        name="payment_reverse",
    ),
    # --- Stripe ------------------------------------------------------------
    path("billing/stripe/webhook/", views.stripe_webhook, name="stripe_webhook"),
]
