"""
Who may go near an invoice.

tests/test_access_matrix.py asks whether a route is reachable by an actor who is
legitimately connected to the case. This file asks what happens when they are not,
and it is where the two halves of ``financial_admin`` are asserted against each
other — because "billing sees the money and never the counseling" is a claim about
two apps at once, and neither app's own tests can make it.

Four properties carry it:

  * **Spouses are billed separately.** On a couple's case each invoice is addressed
    to one person, and the other cannot see it exists. Stricter than documents,
    where a counselor may deliberately share a file with the whole case: an invoice
    has no shared setting at all.
  * **financial_admin sees every invoice and no counseling.** The same actor, in the
    same session, allowed everywhere in billing and refused on every documents and
    messaging route. Asserted in one place so the pair cannot drift.
  * **A counselor reads and never writes.** They may see the bills on their own
    cases, because a counselee will ask; they may not raise, send, void, or take
    money on one.
  * **A draft is invisible to the person it is about**, and the refusal is a 403
    rather than a 404 — the one place the scoping layer and the permission layer
    deliberately disagree. See ``views.visible_invoice_or_404``.

Everything goes through the real views and the real querysets. A test asserting on
a hand-built queryset would pass while a view called ``.objects.all()``.
"""

from datetime import timedelta

import pytest
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.billing import services
from apps.billing.models import (
    Fee,
    FeeKind,
    Invoice,
    InvoiceLineItem,
    Payment,
    PaymentMethod,
    SessionRecord,
    StripeCustomer,
)
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember

pytestmark = pytest.mark.django_db


@pytest.fixture
def ministry_rate(db):
    return Fee.objects.create(
        kind=FeeKind.SESSION,
        amount_cents=8500,
        effective_from=org_today() - timedelta(days=365),
    )


@pytest.fixture
def couple_case(counselor, make_user, financial_admin, ministry_rate):
    """One counselor, two counselees, and a bill addressed to each.

    The couple's case is the shape that makes isolation non-trivial, so it is the
    default here rather than a special case. Both invoices are issued: a draft is
    invisible even to its own payer, and that is asserted separately below.
    """
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage", kind="couple")
    ada = make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")
    ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=ada)
    CaseMember.objects.create(case=case, counselee=ben)

    def _bill(counselee):
        session = services.record_session(case=case, counselee=counselee, actor=financial_admin)
        return services.issue_invoice(
            services.create_invoice(
                case=case, counselee=counselee, sessions=[session], actor=financial_admin
            ),
            actor=financial_admin,
        )

    return case, ada, ben, _bill(ada), _bill(ben)


class TestSpousesOnOneCase:
    """One invoice is addressed to one person. The rest follows from that."""

    def test_neither_sees_the_others_bill_in_a_queryset(self, couple_case):
        _case, ada, ben, hers, his = couple_case

        assert list(Invoice.objects.for_actor(ada)) == [hers]
        assert list(Invoice.objects.for_actor(ben)) == [his]

    def test_the_others_invoice_is_a_404_and_not_a_403(self, client, sign_in, couple_case):
        """404, so the existence of the other's bill is never confirmed.

        A 403 would tell Ada that the ministry has billed Ben for something, and how
        much is owed on their case is not hers to be told by this system.
        """
        _case, ada, _ben, _hers, his = couple_case
        sign_in(ada)

        assert (
            client.get(reverse("billing:invoice_detail", kwargs={"pk": his.pk})).status_code == 404
        )

    def test_the_lines_and_payments_are_invisible_too(self, couple_case, financial_admin):
        """Scoped through the invoice rather than restated, which is what keeps them
        from disagreeing with it."""
        _case, ada, _ben, hers, his = couple_case
        services.record_payment(
            his, amount_cents=1000, method=PaymentMethod.CHECK, actor=financial_admin
        )

        lines = InvoiceLineItem.objects.for_actor(ada)
        payments = Payment.objects.for_actor(ada)

        assert set(lines.values_list("invoice", flat=True)) == {hers.pk}
        assert not payments.filter(invoice=his).exists()

    def test_the_others_sessions_are_invisible(self, couple_case):
        """The evidence behind a bill is scoped as tightly as the bill.

        Sessions are what invoice lines are drawn from, so a spouse who could list
        them could count the other's appointments and price them.
        """
        _case, ada, _ben, _hers, _his = couple_case

        visible = SessionRecord.objects.for_actor(ada)

        assert visible.count() == 1
        assert visible.first().counselee == ada

    def test_the_case_page_lists_only_their_own(self, client, sign_in, couple_case):
        case, ada, _ben, hers, his = couple_case
        sign_in(ada)

        response = client.get(reverse("billing:case_invoices", kwargs={"case_pk": case.pk}))

        assert list(response.context["invoices"]) == [hers]
        assert his.number not in response.content.decode()

    def test_neither_can_be_told_how_much_the_other_owes(self, client, sign_in, couple_case):
        """No total for the case, only for the person. A case-wide figure would be a
        disclosure dressed up as a summary."""
        _case, ada, _ben, hers, his = couple_case
        sign_in(ada)

        body = client.get(reverse("billing:my_invoices")).content.decode()

        assert hers.number in body
        assert his.number not in body
        assert reverse("billing:invoice_detail", kwargs={"pk": his.pk}) not in body

    def test_neither_can_start_paying_the_others(self, client, sign_in, couple_case):
        """Not merely refused — invisible. A payer being able to settle their spouse's
        bill would also tell them it existed."""
        _case, ada, _ben, _hers, his = couple_case
        sign_in(ada)

        assert client.post(reverse("billing:pay", kwargs={"pk": his.pk})).status_code == 404

    def test_the_counselor_sees_both(self, couple_case, counselor):
        _case, _ada, _ben, hers, his = couple_case

        assert set(Invoice.objects.for_actor(counselor)) == {hers, his}


class TestADraftIsNobodysBusinessButTheOffices:
    @pytest.fixture
    def draft(self, couple_case, financial_admin):
        case, ada, *_ = couple_case
        session = services.record_session(case=case, counselee=ada, actor=financial_admin)
        return services.create_invoice(
            case=case, counselee=ada, sessions=[session], actor=financial_admin
        )

    def test_the_payer_is_refused_rather_than_told_it_does_not_exist(
        self, client, sign_in, couple_case, draft
    ):
        """403, and deliberately not 404: the row *is* in Ada's queryset, because it
        is her bill. Whether an amount the office is still working out may be shown to
        her is a question about state, and that answer comes from rules.py."""
        _case, ada, *_ = couple_case
        sign_in(ada)

        assert (
            client.get(reverse("billing:invoice_detail", kwargs={"pk": draft.pk})).status_code
            == 403
        )

    def test_it_is_not_on_their_list_of_bills(self, client, sign_in, couple_case, draft):
        _case, ada, *_ = couple_case
        sign_in(ada)

        body = client.get(reverse("billing:my_invoices")).content.decode()

        assert draft.number not in body

    def test_nor_on_the_case_page_they_can_open(self, client, sign_in, couple_case, draft):
        case, ada, *_ = couple_case
        sign_in(ada)

        body = client.get(
            reverse("billing:case_invoices", kwargs={"case_pk": case.pk})
        ).content.decode()

        assert draft.number not in body

    def test_they_cannot_pay_it_either(self, client, sign_in, couple_case, draft):
        _case, ada, *_ = couple_case
        sign_in(ada)

        assert client.post(reverse("billing:pay", kwargs={"pk": draft.pk})).status_code == 403

    def test_the_office_can_see_it(self, client, sign_in, financial_admin, draft):
        sign_in(financial_admin)

        assert (
            client.get(reverse("billing:invoice_detail", kwargs={"pk": draft.pk})).status_code
            == 200
        )

    def test_and_a_counselee_seeing_a_draft_would_be_audited_as_a_refusal(
        self, client, sign_in, couple_case, draft
    ):
        _case, ada, *_ = couple_case
        sign_in(ada)

        client.get(reverse("billing:invoice_detail", kwargs={"pk": draft.pk}))

        assert AuditEvent.objects.filter(
            verb=AuditVerb.ACCESS_DENIED, actor=ada, metadata__permission="billing.view_invoice"
        ).exists()


class TestAnotherCounselorsCase:
    def test_they_cannot_see_the_invoice_exists(self, couple_case, other_counselor):
        assert list(Invoice.objects.for_actor(other_counselor)) == []
        assert list(SessionRecord.objects.for_actor(other_counselor)) == []

    def test_the_invoice_is_a_404(self, client, sign_in, couple_case, other_counselor):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(other_counselor)

        assert (
            client.get(reverse("billing:invoice_detail", kwargs={"pk": hers.pk})).status_code == 404
        )

    def test_the_case_invoices_page_is_a_404_because_the_case_is_invisible(
        self, client, sign_in, couple_case, other_counselor
    ):
        case, *_ = couple_case
        sign_in(other_counselor)

        response = client.get(reverse("billing:case_invoices", kwargs={"case_pk": case.pk}))

        assert response.status_code == 404


class TestACounselorReadsAndNeverWrites:
    """The conflict of interest this separation exists to prevent: the person in the
    room negotiating money with the person they are counseling."""

    def test_they_can_open_a_bill_on_their_own_case(self, client, sign_in, couple_case, counselor):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(counselor)

        assert (
            client.get(reverse("billing:invoice_detail", kwargs={"pk": hers.pk})).status_code == 200
        )

    def test_the_page_offers_them_nothing_to_click(self, client, sign_in, couple_case, counselor):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(counselor)

        response = client.get(reverse("billing:invoice_detail", kwargs={"pk": hers.pk}))

        flags = response.context
        assert not any(
            flags[name]
            for name in (
                "can_change",
                "can_issue",
                "can_void",
                "can_write_off",
                "can_record_payment",
                "can_reverse_payment",
                "can_pay",
            )
        )

    @pytest.mark.parametrize(
        "route",
        ["billing:invoice_void", "billing:invoice_write_off", "billing:payment_record"],
    )
    def test_every_way_of_changing_one_is_refused(
        self, client, sign_in, couple_case, counselor, route
    ):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(counselor)

        assert client.get(reverse(route, kwargs={"pk": hers.pk})).status_code == 403

    def test_they_cannot_raise_one_on_their_own_case(self, client, sign_in, couple_case, counselor):
        case, *_ = couple_case
        sign_in(counselor)

        assert (
            client.get(reverse("billing:invoice_create", kwargs={"case_pk": case.pk})).status_code
            == 403
        )

    def test_they_cannot_record_money_arriving(self, client, sign_in, couple_case, counselor):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(counselor)

        response = client.post(
            reverse("billing:payment_record", kwargs={"pk": hers.pk}),
            {"amount": "85.00", "method": PaymentMethod.CHECK, "received_on": org_today()},
        )

        assert response.status_code == 403
        assert hers.payments.count() == 0

    def test_the_ministrys_rates_are_not_theirs_to_read(self, client, sign_in, counselor):
        """What the ministry charges is not the counselor's decision, and a rate list
        would include how their colleagues are paid. See FeeQuerySet."""
        sign_in(counselor)

        assert client.get(reverse("billing:fees")).status_code == 403
        assert list(Fee.objects.for_actor(counselor)) == []

    def test_and_neither_is_the_ministry_wide_view_of_money(self, client, sign_in, counselor):
        sign_in(counselor)

        assert client.get(reverse("billing:index")).status_code == 403


class TestThePayerCanSeeAndPayAndNothingElse:
    def test_they_cannot_record_their_own_payment(self, client, sign_in, couple_case):
        """ "I paid, mark it paid" is not a permission, it is an assertion."""
        _case, ada, _ben, hers, _his = couple_case
        sign_in(ada)

        response = client.post(
            reverse("billing:payment_record", kwargs={"pk": hers.pk}),
            {"amount": "85.00", "method": PaymentMethod.CASH, "received_on": org_today()},
        )

        assert response.status_code == 403
        assert hers.payments.count() == 0

    def test_they_cannot_write_their_own_bill_off(self, client, sign_in, couple_case):
        _case, ada, _ben, hers, _his = couple_case
        sign_in(ada)

        assert (
            client.get(reverse("billing:invoice_write_off", kwargs={"pk": hers.pk})).status_code
            == 403
        )

    def test_they_cannot_add_a_line_to_it(self, client, sign_in, couple_case):
        _case, ada, _ben, hers, _his = couple_case
        sign_in(ada)

        response = client.post(
            reverse("billing:line_add", kwargs={"pk": hers.pk}),
            {"description": "A discount", "amount": "-50.00", "quantity": 1},
        )

        assert response.status_code == 403
        assert hers.lines.count() == 1

    def test_they_cannot_reprice_the_session_behind_it(self, client, sign_in, couple_case):
        _case, ada, *_ = couple_case
        session = SessionRecord.objects.for_actor(ada).first()
        sign_in(ada)

        assert (
            client.get(reverse("billing:session_amend", kwargs={"pk": session.pk})).status_code
            == 403
        )

    def test_the_office_page_sends_them_to_their_own_bills_rather_than_refusing(
        self, client, sign_in, couple_case
    ):
        """The wrong door, not a refusal — the same way counseling:my_cases treats
        staff who open it."""
        _case, ada, *_ = couple_case
        sign_in(ada)

        response = client.get(reverse("billing:index"))

        assert response.status_code == 302
        assert response["Location"] == reverse("billing:my_invoices")

    def test_and_staff_opening_the_payers_page_are_sent_the_other_way(
        self, client, sign_in, financial_admin
    ):
        sign_in(financial_admin)

        response = client.get(reverse("billing:my_invoices"))

        assert response.status_code == 302
        assert response["Location"] == reverse("billing:index")


class TestFinancialAdmin:
    """The role this application was shaped around.

    Everything in billing, nothing in counseling. Both halves are asserted here
    together, because each app's own tests can only see one of them.
    """

    def test_they_see_every_invoice_on_every_case(self, couple_case, financial_admin):
        _case, _ada, _ben, hers, his = couple_case

        assert set(Invoice.objects.for_actor(financial_admin)) == {hers, his}

    def test_and_who_each_counselor_is_seeing(self, couple_case, financial_admin):
        """What the instruction asked for in as many words: billing knows which
        counselees a counselor has. It is the case list, not its contents."""
        case, *_ = couple_case

        visible = Case.objects.for_actor(financial_admin)

        assert list(visible) == [case]
        assert visible.first().counselor == case.counselor

    def test_but_not_one_document_on_it(self, client, sign_in, couple_case, financial_admin):
        from apps.documents.models import Document

        case, *_ = couple_case
        sign_in(financial_admin)

        assert list(Document.objects.for_actor(financial_admin)) == []
        assert (
            client.get(reverse("documents:case_documents", kwargs={"case_pk": case.pk})).status_code
            == 403
        )

    def test_nor_one_word_of_the_correspondence(
        self, client, sign_in, couple_case, financial_admin
    ):
        from apps.messaging.models import Message, Thread

        case, *_ = couple_case
        sign_in(financial_admin)

        assert list(Thread.objects.for_actor(financial_admin)) == []
        assert list(Message.objects.for_actor(financial_admin)) == []
        assert (
            client.get(reverse("messaging:case_threads", kwargs={"case_pk": case.pk})).status_code
            == 403
        )

    def test_nor_the_counselors_note_on_a_session_they_are_billing(
        self, client, sign_in, couple_case, financial_admin, counselor
    ):
        """The sharpest version of the separation: the appointment they are charging
        for, and the note about what was said in it. There is nowhere in billing for
        that note to appear, which is why SessionRecord has no notes field at all.
        """
        from django.utils import timezone

        from apps.scheduling.models import Booking, BookingStatus

        case, ada, *_ = couple_case
        booking = Booking.objects.create(
            counselor=counselor,
            case=case,
            counselee=ada,
            slot=Booking.range_for(timezone.now() - timedelta(days=2), 60),
            status=BookingStatus.CONFIRMED,
            created_by=counselor,
            counselor_note="She disclosed something serious.",
        )
        session = services.record_session(
            case=case,
            counselee=ada,
            booking=booking,
            actor=financial_admin,
        )
        sign_in(financial_admin)

        response = client.get(reverse("billing:case_invoices", kwargs={"case_pk": case.pk}))

        # The session is theirs to bill — it is on the page, priced. What was said in
        # it is not, and there is nowhere on the row for it to be: no field on
        # SessionRecord holds a note, so this is a fact about the schema rather than
        # about what a template happens to render today.
        assert session in list(response.context["sessions"])
        assert "disclosed" not in response.content.decode()
        assert [f.name for f in SessionRecord._meta.get_fields() if "note" in f.name] == []

    def test_they_can_do_the_whole_job(self, client, sign_in, couple_case, financial_admin):
        case, ada, _ben, hers, _his = couple_case
        sign_in(financial_admin)

        for url in (
            reverse("billing:index"),
            reverse("billing:fees"),
            reverse("billing:fee_add"),
            reverse("billing:invoice_create", kwargs={"case_pk": case.pk}),
            reverse("billing:invoice_detail", kwargs={"pk": hers.pk}),
            reverse("billing:payment_record", kwargs={"pk": hers.pk}),
            reverse("billing:invoice_void", kwargs={"pk": hers.pk}),
        ):
            assert client.get(url).status_code == 200, url

    def test_the_case_page_offers_them_billing_and_nothing_else(
        self, client, sign_in, couple_case, financial_admin
    ):
        case, *_ = couple_case
        sign_in(financial_admin)

        body = client.get(
            reverse("counseling:case_detail", kwargs={"pk": case.pk})
        ).content.decode()

        assert reverse("billing:case_invoices", kwargs={"case_pk": case.pk}) in body
        assert reverse("documents:case_documents", kwargs={"case_pk": case.pk}) not in body
        assert reverse("messaging:case_threads", kwargs={"case_pk": case.pk}) not in body


class TestWhatHasLeftTheBuilding:
    """Whether the ministry has told a payment processor about somebody.

    One of the very few facts in this system that is not about counseling and is
    still nobody's business but the office's — which is why a counselor is refused it
    while the counselee it concerns is not.
    """

    @pytest.fixture
    def stripe_customer(self, couple_case):
        _case, ada, *_ = couple_case
        return StripeCustomer.objects.create(
            counselee=ada, stripe_customer_id="cus_123", email=ada.email
        )

    def test_billing_sees_it(self, stripe_customer, financial_admin):
        assert list(StripeCustomer.objects.for_actor(financial_admin)) == [stripe_customer]

    def test_the_counselee_it_concerns_sees_it(self, stripe_customer, couple_case):
        _case, ada, *_ = couple_case

        assert list(StripeCustomer.objects.for_actor(ada)) == [stripe_customer]

    def test_the_other_spouse_does_not(self, stripe_customer, couple_case):
        _case, _ada, ben, *_ = couple_case

        assert list(StripeCustomer.objects.for_actor(ben)) == []

    def test_and_neither_does_their_counselor(self, stripe_customer, counselor):
        assert list(StripeCustomer.objects.for_actor(counselor)) == []


class TestSignedOutAndDeactivated:
    def test_an_invoice_is_a_redirect_to_the_login_page_and_not_a_404(self, client, couple_case):
        """Anonymous is sent to sign in rather than told the row is missing: the
        distinction only matters to somebody who is entitled to it."""
        _case, _ada, _ben, hers, _his = couple_case

        response = client.get(reverse("billing:invoice_detail", kwargs={"pk": hers.pk}))

        assert response.status_code == 302
        assert "/login/" in response["Location"]

    def test_a_deactivated_counselee_reaches_nothing(self, couple_case):
        """The bill still stands and the office can chase it by post. What they cannot
        do is sign in — which is decided by ``for_actor`` and not by the login view
        alone, so it is asserted at the queryset."""
        _case, ada, *_ = couple_case
        ada.is_active = False
        ada.save(update_fields=["is_active"])

        assert list(Invoice.objects.for_actor(ada)) == []
        assert list(SessionRecord.objects.for_actor(ada)) == []

    def test_the_database_refuses_a_role_nobody_has_defined(self, couple_case, make_user):
        """The first answer to "what happens when a fifth role is added": it cannot be
        added by accident, because ``user_role_is_known`` is a check constraint."""
        from django.db import IntegrityError, transaction

        stranger = make_user(Role.COUNSELEE)
        stranger.role = "receptionist"

        with pytest.raises(IntegrityError), transaction.atomic():
            stranger.save(update_fields=["role"])

    def test_and_a_role_nobody_has_yet_would_get_nothing(self, couple_case, make_user):
        """The second answer, and the one that matters when the constraint is widened
        by a migration before anybody writes the scoping rules: every ``scope_for_*``
        hook defaults to ``none()``, so the dispatch denies until somebody decides
        otherwise.

        Asserted against an unsaved user for the reason above — the row cannot exist,
        but the dispatch can still be asked what it would do with it.
        """
        _case, _ada, _ben, hers, _his = couple_case
        stranger = make_user(Role.COUNSELEE)
        stranger.role = "receptionist"  # in memory only; never saved

        assert list(Invoice.objects.for_actor(stranger)) == []
        assert list(Fee.objects.for_actor(stranger)) == []
        assert list(Payment.objects.for_actor(stranger)) == []
        assert list(SessionRecord.objects.for_actor(stranger)) == []
        assert list(StripeCustomer.objects.for_actor(stranger)) == []
