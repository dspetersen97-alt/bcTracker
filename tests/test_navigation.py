"""
Every feature a role has is reachable by clicking.

This file exists because self-booking shipped with no way in. The view was there,
the permission was there, the slot arithmetic was tested from four directions, and
`tests/test_access_matrix.py` asserted the route answered 200 for a counselee — and
no page a counselee could open contained a link to it. The feature worked perfectly
and did not exist.

That is the gap between an access matrix and a usable application. A matrix asks
"may this actor reach this URL", and answers by *going* to the URL. It cannot
notice that nothing led there, because it never needed a link to begin with.

So the assertions here are the other half: for each role, open the pages that role
lands on and require that certain routes appear in the markup. Written as "this URL
is somewhere in the reachable set" rather than against particular templates, so
moving a button between two pages is not a failure — losing it is.

Deliberately not a crawl of every link on every page. The point is a short,
explicit list of the things a role must be able to *start*, which is a product
decision worth stating once in a place that fails when it stops being true.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import Role
from apps.counseling.models import Case, CaseMember, CaseStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def joel(make_user):
    return make_user(Role.COUNSELEE, first_name="Joel", last_name="Marsh")


@pytest.fixture
def joels_case(counselor, joel):
    case = Case.objects.create(counselor=counselor, label="Marsh — individual")
    CaseMember.objects.create(case=case, counselee=joel)
    return case


def markup_of(client, urls):
    """Everything a role sees across the pages they land on, as one string.

    Concatenated on purpose: the question is whether the link is anywhere in what
    they can reach, not which page happens to hold it today.
    """
    seen = []
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, f"{url} answered {response.status_code}"
        seen.append(response.content.decode())
    return "\n".join(seen)


class TestACounseleeCanFindTheirWayToBooking:
    """The regression. Two entry points, because both are pages they open."""

    @pytest.fixture
    def landing_pages(self):
        return [reverse("counseling:my_cases"), reverse("scheduling:appointments")]

    def test_the_booking_page_is_linked(self, client, sign_in, joel, joels_case, landing_pages):
        sign_in(joel)

        markup = markup_of(client, landing_pages)

        assert reverse("scheduling:book", args=[joels_case.pk]) in markup, (
            "a counselee has no way to book an appointment except by typing the URL"
        )

    def test_the_diary_offers_it_too(self, client, sign_in, joel, joels_case):
        """Asserted separately, because the diary is where somebody goes *looking*.

        "My appointments" is the page you open when you want a next appointment. A
        link on the case list only is a link most people will not find.
        """
        sign_in(joel)

        response = client.get(reverse("scheduling:appointments"))

        assert reverse("scheduling:book", args=[joels_case.pk]) in response.content.decode()

    def test_the_link_leads_somewhere_that_answers(
        self, client, sign_in, joel, joels_case, landing_pages
    ):
        """Following it, rather than trusting that a reversed URL means a live page."""
        sign_in(joel)
        markup_of(client, landing_pages)

        response = client.get(reverse("scheduling:book", args=[joels_case.pk]))

        assert response.status_code == 200

    def test_a_closed_case_is_not_offered(self, client, sign_in, joel, joels_case, landing_pages):
        """A button leading to a refusal is worse than no button.

        The case list already draws this line for messaging; booking follows it.
        """
        joels_case.close()
        sign_in(joel)

        markup = markup_of(client, landing_pages)

        assert reverse("scheduling:book", args=[joels_case.pk]) not in markup

    def test_a_case_on_hold_is_not_offered(self, client, sign_in, joel, joels_case, landing_pages):
        """On hold means the counseling has paused. Writing stays open; booking does not."""
        joels_case.status = CaseStatus.ON_HOLD
        joels_case.save(update_fields=["status", "updated_at"])
        sign_in(joel)

        markup = markup_of(client, landing_pages)

        assert reverse("scheduling:book", args=[joels_case.pk]) not in markup
        assert reverse("messaging:start", args=[joels_case.pk]) in markup

    def test_a_former_member_is_offered_nothing(
        self, client, sign_in, joel, joels_case, landing_pages
    ):
        """Access ends with the relationship, on the buttons as well as the pages."""
        CaseMember.objects.filter(case=joels_case, counselee=joel).update(
            ended_on=joels_case.opened_on
        )
        sign_in(joel)

        markup = markup_of(client, landing_pages)

        assert reverse("scheduling:book", args=[joels_case.pk]) not in markup


class TestACounselorIsOfferedTheirOwnRoutes:
    """Not the same button: a counselor books somebody else in, via ``schedule``."""

    def test_they_are_not_offered_self_booking_on_the_diary(
        self, client, sign_in, counselor, joels_case
    ):
        """``book`` refuses a counselor's POST — they cannot be their own counselee."""
        sign_in(counselor)

        markup = client.get(reverse("scheduling:appointments")).content.decode()

        assert reverse("scheduling:book", args=[joels_case.pk]) not in markup

    def test_they_can_reach_office_hours_and_the_case_diary(
        self, client, sign_in, counselor, joels_case
    ):
        sign_in(counselor)

        markup = markup_of(
            client,
            [
                reverse("scheduling:appointments"),
                reverse("counseling:case_detail", args=[joels_case.pk]),
            ],
        )

        assert reverse("scheduling:availability") in markup
        assert reverse("scheduling:case_appointments", args=[joels_case.pk]) in markup

    def test_the_case_diary_leads_to_scheduling_somebody_in(
        self, client, sign_in, counselor, joels_case
    ):
        sign_in(counselor)

        markup = client.get(
            reverse("scheduling:case_appointments", args=[joels_case.pk])
        ).content.decode()

        assert reverse("scheduling:schedule", args=[joels_case.pk]) in markup


@pytest.fixture
def a_session_to_bill(db, joels_case, joel):
    """One billable session on Joel's case, priced from a real fee row.

    Billing's whole first screen is "what is waiting", so a navigation test with
    nothing waiting would pass against an empty page.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.billing import services as billing
    from apps.billing.models import Fee, FeeKind

    Fee.objects.create(
        kind=FeeKind.SESSION,
        amount_cents=8500,
        effective_from=timezone.localdate() - timedelta(days=30),
    )
    return billing.record_session(case=joels_case, counselee=joel)


@pytest.fixture
def joels_invoice(a_session_to_bill, joels_case, joel, make_user):
    from apps.billing import services as billing

    biller = make_user(Role.FINANCIAL_ADMIN)
    return billing.issue_invoice(
        billing.create_invoice(
            case=joels_case, counselee=joel, sessions=[a_session_to_bill], actor=biller
        ),
        actor=biller,
    )


class TestBillingIsReachableByClicking:
    """The same claim as the booking regression above, for money.

    An invoice nobody can find is worse than a booking page nobody can find: the
    ministry does not get paid and the counselee does not know why they are being
    chased.
    """

    def test_a_financial_admin_lands_within_reach_of_billing(
        self, client, sign_in, financial_admin
    ):
        sign_in(financial_admin)

        markup = markup_of(client, [reverse("counseling:caseload_index"), reverse("billing:index")])

        assert reverse("billing:index") in markup
        assert reverse("billing:fees") in markup, (
            "nobody can set up the fee schedule, so every session records at nothing"
        )

    def test_billing_leads_to_raising_an_invoice_for_work_that_is_waiting(
        self, client, sign_in, financial_admin, joels_case, a_session_to_bill
    ):
        sign_in(financial_admin)

        markup = markup_of(client, [reverse("billing:index")])

        assert reverse("billing:invoice_create", args=[joels_case.pk]) in markup

    def test_an_admin_can_reach_billing_too(self, client, sign_in, admin_user):
        """Deliberately both roles: a ministry too small for a bookkeeper still bills."""
        sign_in(admin_user)

        markup = markup_of(client, [reverse("counseling:case_list")])

        assert reverse("billing:index") in markup

    def test_a_case_leads_to_its_invoices(self, client, sign_in, counselor, joels_case):
        """The counselor's route: they are asked about a bill, in the room."""
        sign_in(counselor)

        markup = markup_of(client, [reverse("counseling:case_detail", args=[joels_case.pk])])

        assert reverse("billing:case_invoices", args=[joels_case.pk]) in markup

    def test_a_counselee_can_find_their_own_invoices(
        self, client, sign_in, joel, joels_case, joels_invoice
    ):
        sign_in(joel)

        markup = markup_of(client, [reverse("counseling:my_cases")])

        assert reverse("billing:my_invoices") in markup

    def test_and_the_invoice_itself(self, client, sign_in, joel, joels_invoice):
        """A list of bills with no way into one is a list of numbers."""
        sign_in(joel)

        markup = markup_of(client, [reverse("billing:my_invoices")])

        assert reverse("billing:invoice_detail", args=[joels_invoice.pk]) in markup

    def test_the_office_can_reach_an_outstanding_invoice_from_the_billing_page(
        self, client, sign_in, financial_admin, joels_invoice
    ):
        sign_in(financial_admin)

        markup = markup_of(client, [reverse("billing:index")])

        assert reverse("billing:invoice_detail", args=[joels_invoice.pk]) in markup

    def test_an_invoice_offers_the_actions_the_office_needs(
        self, client, sign_in, financial_admin, joels_invoice
    ):
        """Recording a check and withdrawing a wrong invoice are the two the office
        actually does, and neither has any other entry point."""
        sign_in(financial_admin)

        markup = markup_of(client, [reverse("billing:invoice_detail", args=[joels_invoice.pk])])

        assert reverse("billing:payment_record", args=[joels_invoice.pk]) in markup
        assert reverse("billing:invoice_void", args=[joels_invoice.pk]) in markup

    def test_an_unpriced_session_is_offered_a_correction(
        self, client, sign_in, financial_admin, joels_case, joel
    ):
        """The gap that would otherwise be silent: a session recorded before anybody
        set up the fee schedule is billed at nothing, and the billing page is the only
        place that says so."""
        from apps.billing import services as billing

        session = billing.record_session(case=joels_case, counselee=joel)
        assert session.is_billable is False, "no Fee row, so this should not be chargeable"
        sign_in(financial_admin)

        markup = markup_of(client, [reverse("billing:index")])

        assert reverse("billing:session_amend", args=[session.pk]) in markup
