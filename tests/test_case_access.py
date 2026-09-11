"""
Object-level case access — the tests the whole authorization design exists for.

tests/test_access_matrix.py asks "may this role reach this route", with the actor
legitimately connected to the object. This file asks the harder question: what
happens when they are *not*. Two properties carry the product:

  * A counselor cannot reach another counselor's case, and gets 404 rather than
    403 — a 403 would confirm the case exists, which is itself a disclosure.
  * Counselees on a shared case see nothing of each other. This is the decision
    that cost the most to make and is the easiest to regress, because the
    convenient query ("all members of this case") is the wrong one.

Everything here goes through the real views and the real querysets. A test that
asserted on a hand-built queryset would pass while the view called
``.objects.all()``.
"""

import pytest
from django.conf import settings
from django.core import mail
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts import services
from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import (
    Case,
    CaseMember,
    CaseStatus,
    CounseleeProfile,
    CounselorProfile,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def couple_case(counselor, make_user):
    """A shared case: one counselor, two counselees.

    The couple's case is the shape that makes isolation non-trivial, so it is the
    default fixture rather than a special case.
    """
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage", kind="couple")
    first = make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")
    second = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=first)
    CaseMember.objects.create(case=case, counselee=second)
    return case, first, second


class TestCaseVisibility:
    """The queryset layer, asked directly. Views inherit whatever this says."""

    def test_a_counselor_sees_only_their_own_cases(self, counselor, other_counselor, make_user):
        mine = Case.objects.create(counselor=counselor, label="Mine")
        theirs = Case.objects.create(counselor=other_counselor, label="Theirs")

        visible = Case.objects.for_actor(counselor)

        assert list(visible) == [mine]
        assert theirs not in visible

    def test_a_counselee_sees_only_cases_they_are_on(self, couple_case, make_user):
        case, ada, _ben = couple_case
        Case.objects.create(counselor=case.counselor, label="Someone else")

        assert list(Case.objects.for_actor(ada)) == [case]

    def test_an_admin_sees_every_case(self, admin_user, counselor, other_counselor):
        Case.objects.create(counselor=counselor, label="One")
        Case.objects.create(counselor=other_counselor, label="Two")

        assert Case.objects.for_actor(admin_user).count() == 2

    def test_financial_admin_sees_every_case_because_billing_needs_the_list(
        self, financial_admin, counselor
    ):
        """Deliberate: which counselor serves which counselee is billing data.

        What financial_admin must never reach is the *content* — asserted in
        TestWhatFinancialAdminSeesOnACase below and, for documents, in that app.
        """
        Case.objects.create(counselor=counselor, label="One")

        assert Case.objects.for_actor(financial_admin).count() == 1

    def test_ending_a_membership_ends_the_visibility(self, couple_case):
        case, ada, _ben = couple_case

        CaseMember.objects.get(case=case, counselee=ada).end()

        assert not Case.objects.for_actor(ada).exists()

    def test_a_deactivated_counselor_sees_nothing(self, counselor):
        Case.objects.create(counselor=counselor, label="Mine")
        counselor.is_active = False

        assert not Case.objects.for_actor(counselor).exists()

    def test_a_soft_deleted_case_is_invisible_even_to_an_admin(self, admin_user, counselor):
        case = Case.objects.create(counselor=counselor, label="Withdrawn")
        case.soft_delete()

        assert not Case.objects.for_actor(admin_user).exists()
        assert Case.all_objects.filter(pk=case.pk).exists(), "the row must survive for audit"

    def test_withdrawing_a_case_hides_what_hangs_off_it(self, admin_user, couple_case):
        """Soft-deleting a Case must take its related rows with it.

        Enforced once in CaseScopedQuerySet.for_actor rather than per model, so
        documents and bookings inherit it when those apps arrive. Asserted here
        against CaseMember because it is the first model to reach a Case through
        a relation.
        """
        case, _ada, _ben = couple_case
        assert CaseMember.objects.for_actor(admin_user).count() == 2

        case.soft_delete()

        assert not CaseMember.objects.for_actor(admin_user).exists()
        assert CaseMember.objects.filter(case=case).count() == 2, "unscoped access still finds them"


class TestCrossCounselorIsolation:
    """A counselor reaching for a colleague's case gets nothing, and learns nothing."""

    @pytest.fixture
    def foreign_case(self, other_counselor, make_user):
        case = Case.objects.create(counselor=other_counselor, label="Not yours")
        member = CaseMember.objects.create(case=case, counselee=make_user(Role.COUNSELEE))
        return case, member

    def test_detail_is_404_not_403(self, client, counselor, sign_in, foreign_case):
        case, _member = foreign_case
        sign_in(counselor)

        response = client.get(reverse("counseling:case_detail", args=[case.pk]))

        assert response.status_code == 404, (
            "403 would confirm the case exists; the counselor must not learn that"
        )

    @pytest.mark.parametrize("route", ["counseling:case_edit", "counseling:case_member_add"])
    def test_get_routes_are_404(self, client, counselor, sign_in, foreign_case, route):
        case, _member = foreign_case
        sign_in(counselor)

        assert client.get(reverse(route, args=[case.pk])).status_code == 404

    def test_close_is_404(self, client, counselor, sign_in, foreign_case):
        case, _member = foreign_case
        sign_in(counselor)

        assert client.post(reverse("counseling:case_close", args=[case.pk])).status_code == 404
        case.refresh_from_db()
        assert case.status == CaseStatus.ACTIVE

    def test_ending_a_membership_is_404(self, client, counselor, sign_in, foreign_case):
        case, member = foreign_case
        sign_in(counselor)

        args = [case.pk, member.pk]
        assert client.post(reverse("counseling:case_member_end", args=args)).status_code == 404
        member.refresh_from_db()
        assert member.ended_on is None

    def test_the_case_list_does_not_mention_it(self, client, counselor, sign_in, foreign_case):
        case, _member = foreign_case
        Case.objects.create(counselor=counselor, label="Mine")
        sign_in(counselor)

        response = client.get(reverse("counseling:case_list"))

        assert b"Not yours" not in response.content
        assert b"Mine" in response.content


class TestSharedCaseIsolation:
    """“Nothing of the other's”, on the one page both counselees can reach."""

    def test_a_counselee_sees_only_their_own_membership_row(self, couple_case):
        case, ada, ben = couple_case

        rows = CaseMember.objects.for_actor(ada).filter(case=case)

        assert [row.counselee for row in rows] == [ada]
        assert ben not in [row.counselee for row in rows]

    def test_the_case_page_does_not_name_the_other_counselee(self, client, sign_in, couple_case):
        case, ada, ben = couple_case
        sign_in(ada)

        response = client.get(reverse("counseling:case_detail", args=[case.pk]))

        assert response.status_code == 200
        assert list(response.context["members"]) == [
            CaseMember.objects.get(case=case, counselee=ada)
        ]
        assert ben.last_name.encode() in response.content, "shared surname, so check the given name"
        assert b"Ben" not in response.content

    def test_the_counselor_does_see_both(self, client, sign_in, couple_case):
        case, _ada, _ben = couple_case
        sign_in(case.counselor)

        response = client.get(reverse("counseling:case_detail", args=[case.pk]))

        assert response.context["members"].count() == 2
        assert b"Ada" in response.content
        assert b"Ben" in response.content

    def test_a_counselee_cannot_reach_the_others_intake_details(self, client, sign_in, couple_case):
        case, ada, ben = couple_case
        bens_profile = CounseleeProfile.objects.create(user=ben, address="12 Elm Street")
        sign_in(ada)

        response = client.get(
            reverse("counseling:counselee_profile_edit_for", args=[bens_profile.pk])
        )

        assert response.status_code == 403

    def test_my_cases_lists_the_case_without_the_roster(self, client, sign_in, couple_case):
        case, ada, ben = couple_case
        sign_in(ada)

        response = client.get(reverse("counseling:my_cases"))

        assert case.label.encode() in response.content
        assert b"Ben" not in response.content


class TestWhatFinancialAdminSeesOnACase:
    """Billing may confirm a case exists and who carries it. Not what it is about."""

    @pytest.fixture
    def noted_case(self, counselor, make_user):
        case = Case.objects.create(
            counselor=counselor,
            label="Ashford — marriage",
            notes="Presenting concern: infidelity.",
        )
        CaseMember.objects.create(case=case, counselee=make_user(Role.COUNSELEE))
        return case

    def test_the_notes_are_not_on_the_page(self, client, sign_in, financial_admin, noted_case):
        sign_in(financial_admin)

        response = client.get(reverse("counseling:case_detail", args=[noted_case.pk]))

        assert response.status_code == 200
        assert response.context["show_notes"] is False
        assert b"infidelity" not in response.content

    def test_the_counselor_does_see_the_notes(self, client, sign_in, noted_case):
        sign_in(noted_case.counselor)

        response = client.get(reverse("counseling:case_detail", args=[noted_case.pk]))

        assert response.context["show_notes"] is True
        assert b"infidelity" in response.content

    def test_intake_details_are_out_of_reach_entirely(self, financial_admin, couple_case):
        _case, ada, _ben = couple_case
        CounseleeProfile.objects.create(user=ada, address="12 Elm Street")

        assert not CounseleeProfile.objects.for_actor(financial_admin).exists()

    def test_the_caseload_index_counts_without_naming_the_case(
        self, client, sign_in, financial_admin, noted_case
    ):
        sign_in(financial_admin)

        response = client.get(reverse("counseling:caseload_index"))
        rows = list(response.context["rows"])

        assert len(rows) == 1
        assert rows[0]["case_count"] == 1
        assert rows[0]["counselee_count"] == 1
        assert b"infidelity" not in response.content

    def test_the_index_does_not_count_ended_memberships(
        self, client, sign_in, financial_admin, couple_case
    ):
        case, ada, _ben = couple_case
        CaseMember.objects.get(case=case, counselee=ada).end()
        sign_in(financial_admin)

        rows = list(client.get(reverse("counseling:caseload_index")).context["rows"])

        assert rows[0]["counselee_count"] == 1


class TestMembershipLifecycle:
    """Adding a member is the one action here with a disclosure consequence."""

    def test_adding_a_member_grants_access_and_is_audited(
        self, client, sign_in, admin_user, counselor, make_user
    ):
        case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
        newcomer = make_user(Role.COUNSELEE)
        assert not Case.objects.for_actor(newcomer).exists()
        sign_in(admin_user)

        response = client.post(
            reverse("counseling:case_member_add", args=[case.pk]),
            {"counselee": newcomer.pk, "joined_on": "2026-09-01"},
        )

        assert response.status_code == 302
        assert list(Case.objects.for_actor(newcomer)) == [case]
        event = AuditEvent.objects.get(verb=AuditVerb.CASE_MEMBER_ADDED)
        assert event.actor == admin_user
        assert event.target_id == str(case.pk)
        assert event.metadata["counselee_email"] == newcomer.email

    def test_the_form_will_not_offer_someone_already_on_the_case(self, couple_case):
        from apps.counseling.forms import CaseMemberForm

        case, ada, _ben = couple_case

        offered = CaseMemberForm(case=case).fields["counselee"].queryset

        assert ada not in offered

    def test_the_form_will_not_offer_a_counselor_as_a_counselee(self, counselor, make_user):
        from apps.counseling.forms import CaseMemberForm

        case = Case.objects.create(counselor=counselor, label="Solo")

        assert counselor not in CaseMemberForm(case=case).fields["counselee"].queryset

    def test_ending_a_membership_keeps_the_row(self, client, sign_in, admin_user, couple_case):
        case, ada, _ben = couple_case
        member = CaseMember.objects.get(case=case, counselee=ada)
        sign_in(admin_user)

        response = client.post(
            reverse("counseling:case_member_end", args=[case.pk, member.pk]),
        )

        assert response.status_code == 302
        member.refresh_from_db()
        assert member.ended_on is not None, "the dates are the record of who was in the room"
        assert not Case.objects.for_actor(ada).exists()
        assert AuditEvent.objects.filter(verb=AuditVerb.CASE_MEMBER_ENDED).exists()

    def test_ending_it_twice_is_a_404(self, client, sign_in, admin_user, couple_case):
        case, ada, _ben = couple_case
        member = CaseMember.objects.get(case=case, counselee=ada)
        member.end()
        sign_in(admin_user)

        response = client.post(reverse("counseling:case_member_end", args=[case.pk, member.pk]))

        assert response.status_code == 404

    def test_the_same_counselee_cannot_join_twice(self, couple_case):
        case, ada, _ben = couple_case

        with pytest.raises(IntegrityError), transaction.atomic():
            CaseMember.objects.create(case=case, counselee=ada)


class TestCaseLifecycle:
    def test_a_counselor_may_close_their_own_case(self, client, sign_in, counselor):
        case = Case.objects.create(counselor=counselor, label="Concluded")
        sign_in(counselor)

        client.post(reverse("counseling:case_close", args=[case.pk]))

        case.refresh_from_db()
        assert case.status == CaseStatus.CLOSED
        assert case.closed_on is not None
        assert AuditEvent.objects.filter(verb=AuditVerb.CASE_CLOSED).exists()

    def test_a_closed_case_cannot_be_closed_again(self, client, sign_in, counselor):
        case = Case.objects.create(counselor=counselor, label="Concluded")
        case.close()
        sign_in(counselor)

        assert client.post(reverse("counseling:case_close", args=[case.pk])).status_code == 403

    def test_a_closed_case_stays_readable(self, client, sign_in, couple_case):
        case, ada, _ben = couple_case
        case.close()
        sign_in(ada)

        assert client.get(reverse("counseling:case_detail", args=[case.pk])).status_code == 200

    def test_a_counselor_cannot_reassign_their_own_case(
        self, client, sign_in, counselor, other_counselor
    ):
        """Reassignment moves who can read the documents, so it stays an admin's call.

        The field is absent from the form rather than merely ignored, but posting
        it anyway must not work either — that is what this asserts.
        """
        case = Case.objects.create(counselor=counselor, label="Mine")
        sign_in(counselor)

        client.post(
            reverse("counseling:case_edit", args=[case.pk]),
            {"label": "Mine", "kind": "individual", "notes": "", "counselor": other_counselor.pk},
        )

        case.refresh_from_db()
        assert case.counselor == counselor

    def test_an_admin_reassigning_is_recorded_with_both_counselors(
        self, client, sign_in, admin_user, counselor, other_counselor
    ):
        case = Case.objects.create(counselor=counselor, label="Mine")
        sign_in(admin_user)

        client.post(
            reverse("counseling:case_edit", args=[case.pk]),
            {"label": "Mine", "kind": "individual", "notes": "", "counselor": other_counselor.pk},
        )

        case.refresh_from_db()
        assert case.counselor == other_counselor
        event = AuditEvent.objects.get(verb=AuditVerb.CASE_REASSIGNED)
        assert event.metadata["previous_counselor_id"] == counselor.pk
        assert event.metadata["counselor_id"] == other_counselor.pk

    def test_reassignment_takes_the_old_counselors_access_with_it(
        self, client, sign_in, admin_user, counselor, other_counselor
    ):
        case = Case.objects.create(counselor=counselor, label="Mine")
        sign_in(admin_user)

        client.post(
            reverse("counseling:case_edit", args=[case.pk]),
            {"label": "Mine", "kind": "individual", "notes": "", "counselor": other_counselor.pk},
        )

        assert not Case.objects.for_actor(counselor).exists()
        assert Case.objects.for_actor(other_counselor).exists()

    def test_a_case_cannot_be_assigned_to_a_non_counselor(self, financial_admin):
        from django.core.exceptions import ValidationError

        case = Case(counselor=financial_admin, label="Wrong")

        with pytest.raises(ValidationError) as caught:
            case.full_clean()
        assert "counselor" in caught.value.error_dict

    def test_the_form_only_offers_counselors(self, counselor, financial_admin, counselee):
        from apps.counseling.forms import CaseForm

        offered = CaseForm().fields["counselor"].queryset

        assert counselor in offered
        assert financial_admin not in offered
        assert counselee not in offered

    def test_the_form_will_not_offer_a_deactivated_counselor(self, counselor):
        from apps.counseling.forms import CaseForm

        counselor.is_active = False
        counselor.save(update_fields=["is_active"])

        assert counselor not in CaseForm().fields["counselor"].queryset

    def test_nobody_may_delete_a_case(self, admin_user, superuser, counselor):
        """Cases are closed and retained; the permission exists only to say no."""
        case = Case.objects.create(counselor=counselor, label="Mine")

        assert admin_user.has_perm("counseling.delete_case", case) is False
        assert counselor.has_perm("counseling.delete_case", case) is False


class TestCaseConstraints:
    """Rules the database enforces, so buggy future code cannot violate them."""

    def test_a_closed_case_must_have_a_closed_date(self, counselor):
        with pytest.raises(IntegrityError), transaction.atomic():
            Case.objects.create(counselor=counselor, label="Bad", status=CaseStatus.CLOSED)

    def test_a_case_cannot_close_before_it_opened(self, counselor):
        with pytest.raises(IntegrityError), transaction.atomic():
            Case.objects.create(
                counselor=counselor,
                label="Bad",
                opened_on="2026-03-01",
                closed_on="2026-02-01",
                status=CaseStatus.CLOSED,
            )

    def test_a_membership_cannot_end_before_it_began(self, counselor, counselee):
        case = Case.objects.create(counselor=counselor, label="Mine")

        with pytest.raises(IntegrityError), transaction.atomic():
            CaseMember.objects.create(
                case=case, counselee=counselee, joined_on="2026-03-01", ended_on="2026-02-01"
            )

    def test_only_a_counselee_can_be_a_member(self, counselor, other_counselor):
        from django.core.exceptions import ValidationError

        case = Case.objects.create(counselor=counselor, label="Mine")
        member = CaseMember(case=case, counselee=other_counselor)

        with pytest.raises(ValidationError) as caught:
            member.full_clean()
        assert "counselee" in caught.value.error_dict

    def test_an_implausible_session_length_is_refused(self, counselor):
        with pytest.raises(IntegrityError), transaction.atomic():
            CounselorProfile.objects.create(user=counselor, default_session_minutes=5)


class TestCounseleeCreation:
    """Creating the account and inviting them is one action, or neither happens."""

    def _payload(self, **overrides):
        return {
            "first_name": "Ada",
            "last_name": "Ashford",
            "email": "ada@example.org",
            "phone": "",
            "allow_magic_link": "on",
        } | overrides

    def test_an_admin_creates_an_account_a_profile_and_an_invitation(
        self, client, sign_in, admin_user
    ):
        sign_in(admin_user)

        response = client.post(reverse("counseling:counselee_create"), self._payload())

        assert response.status_code == 302
        user = User.objects.get(email="ada@example.org")
        assert user.role == Role.COUNSELEE
        assert user.has_usable_password() is False, "the password is set from the invitation"
        assert CounseleeProfile.objects.filter(user=user).exists()
        assert len(mail.outbox) == 1
        assert user.email in mail.outbox[0].to
        assert AuditEvent.objects.filter(verb=AuditVerb.USER_CREATED, actor=admin_user).exists()

    def test_an_address_already_in_use_is_refused(self, client, sign_in, admin_user, counselee):
        sign_in(admin_user)

        response = client.post(
            reverse("counseling:counselee_create"),
            self._payload(email=counselee.email.upper()),
        )

        assert response.status_code == 200
        assert "email" in response.context["form"].errors
        assert User.objects.filter(role=Role.COUNSELEE).count() == 1

    def test_a_failed_invitation_hands_the_link_over_instead_of_erroring(
        self, client, sign_in, admin_user, monkeypatch
    ):
        """If the invitation cannot be sent, the account stands and the link is shown.

        This used to roll the account back, on the reasoning that an address taken
        by a login nobody can reach is worse than a failed form. That reasoning
        only held while the link could not be recovered — and it turned a
        deployment whose app password had been revoked into a server error on the
        one page that takes on a new counselee. Handing the administrator the link
        satisfies both concerns: the account exists and it is claimable.
        """

        def explode(**kwargs):
            raise RuntimeError("SMTP is down")

        monkeypatch.setattr(services, "_send_invitation_email", explode)
        sign_in(admin_user)

        response = client.post(reverse("counseling:counselee_create"), self._payload())

        assert response.status_code == 200
        user = User.objects.get(email="ada@example.org")
        assert CounseleeProfile.objects.filter(user=user).exists()
        assert not mail.outbox
        # The link is on the page, and it is one that actually works.
        link = response.context["invitation_link"]
        assert client.get(link.replace(settings.SITE_BASE_URL, "")).status_code == 200
        # And the trail says the account was created without the email arriving.
        event = AuditEvent.objects.get(verb=AuditVerb.INVITATION_SENT, target_id=str(user.pk))
        assert event.metadata["emailed"] is False
        assert event.metadata["delivery_error"] == "RuntimeError"

    @pytest.mark.parametrize("role", [Role.COUNSELOR, Role.FINANCIAL_ADMIN, Role.COUNSELEE])
    def test_nobody_else_may_create_one(self, client, sign_in, make_user, role):
        sign_in(make_user(role))

        response = client.post(reverse("counseling:counselee_create"), self._payload())

        assert response.status_code == 403
        assert not User.objects.filter(email="ada@example.org").exists()


class TestProfileAccess:
    def test_a_counselor_may_correct_the_intake_of_someone_on_their_case(
        self, client, sign_in, couple_case
    ):
        case, ada, _ben = couple_case
        profile = CounseleeProfile.objects.create(user=ada)
        sign_in(case.counselor)

        response = client.post(
            reverse("counseling:counselee_profile_edit_for", args=[profile.pk]),
            {
                "date_of_birth": "1985-04-02",
                "address": "12 Elm Street",
                "emergency_contact_name": "Ben Ashford",
                "emergency_contact_phone": "555-0100",
                "referred_by": "",
            },
        )

        assert response.status_code == 302
        profile.refresh_from_db()
        assert profile.address == "12 Elm Street"
        event = AuditEvent.objects.get(verb=AuditVerb.PROFILE_UPDATED)
        assert event.target_id == str(ada.pk), "the audit names the person, not the profile row"

    def test_a_counselor_cannot_reach_the_intake_of_someone_elses_counselee(
        self, client, sign_in, counselor, other_counselor, make_user
    ):
        foreign = make_user(Role.COUNSELEE)
        CaseMember.objects.create(
            case=Case.objects.create(counselor=other_counselor, label="Theirs"),
            counselee=foreign,
        )
        profile = CounseleeProfile.objects.create(user=foreign)
        sign_in(counselor)

        response = client.get(reverse("counseling:counselee_profile_edit_for", args=[profile.pk]))

        assert response.status_code == 404

    def test_access_ends_when_the_membership_does(self, client, sign_in, couple_case):
        case, ada, _ben = couple_case
        profile = CounseleeProfile.objects.create(user=ada)
        CaseMember.objects.get(case=case, counselee=ada).end()
        sign_in(case.counselor)

        response = client.get(reverse("counseling:counselee_profile_edit_for", args=[profile.pk]))

        assert response.status_code == 404

    def test_a_counselee_maintains_their_own(self, client, sign_in, counselee):
        sign_in(counselee)

        response = client.post(
            reverse("counseling:counselee_profile_edit"),
            {
                "date_of_birth": "",
                "address": "12 Elm Street",
                "emergency_contact_name": "",
                "emergency_contact_phone": "",
                "referred_by": "A friend",
            },
        )

        assert response.status_code == 302
        profile = CounseleeProfile.objects.get(user=counselee)
        assert profile.referred_by == "A friend"

    def test_the_page_is_created_on_first_visit(self, client, sign_in, counselee):
        sign_in(counselee)

        assert client.get(reverse("counseling:counselee_profile_edit")).status_code == 200
        assert CounseleeProfile.objects.filter(user=counselee).exists()

    def test_a_counselor_edits_their_own_practice_settings(self, client, sign_in, counselor):
        sign_in(counselor)

        response = client.post(
            reverse("counseling:counselor_profile_edit"),
            {
                "credentials": "ACBC Certified",
                "bio": "",
                "default_session_minutes": 50,
                "booking_notice_hours": 48,
                "booking_horizon_days": 30,
                "accepting_new_cases": "on",
            },
        )

        assert response.status_code == 302
        profile = CounselorProfile.objects.get(user=counselor)
        assert profile.credentials == "ACBC Certified"
        assert profile.default_session_minutes == 50

    def test_an_admin_has_no_route_to_a_counselors_practice_settings(
        self, client, sign_in, admin_user, counselor
    ):
        """Not an oversight: the URL has no counselor id, so it cannot mean anyone else.

        If the ministry later needs an admin to set these, that is a new route
        with an id in it and a permission of its own — not a widening of this one.
        """
        sign_in(admin_user)

        assert client.get(reverse("counseling:counselor_profile_edit")).status_code == 403

    def test_a_counselee_may_read_a_counselors_credentials(self, counselee, counselor):
        """They pick an appointment from these, so every signed-in role may read them."""
        CounselorProfile.objects.create(user=counselor, credentials="ACBC Certified")

        assert CounselorProfile.objects.for_actor(counselee).count() == 1


class TestTheAuditTrailOnCaseAccess:
    """A trail covering only downloads cannot answer who looked at whose file."""

    def test_reading_a_case_is_recorded(self, client, sign_in, couple_case):
        case, ada, _ben = couple_case
        sign_in(ada)

        client.get(reverse("counseling:case_detail", args=[case.pk]))

        event = AuditEvent.objects.get(verb=AuditVerb.CASE_VIEWED)
        assert event.actor == ada
        assert event.actor_role == Role.COUNSELEE, "the role is snapshotted, not looked up later"
        assert event.target_type == "counseling.Case"
        assert event.target_id == str(case.pk)

    def test_a_refusal_is_recorded_with_the_permission_that_refused_it(
        self, client, sign_in, couple_case
    ):
        case, ada, _ben = couple_case
        sign_in(ada)

        client.get(reverse("counseling:case_edit", args=[case.pk]))

        event = AuditEvent.objects.get(verb=AuditVerb.ACCESS_DENIED)
        assert event.actor == ada
        assert event.metadata["permission"] == "counseling.change_case"

    def test_a_404_on_a_foreign_case_records_nothing_about_that_case(
        self, client, sign_in, counselor, other_counselor
    ):
        """The scoping layer refuses before the permission check, so there is no

        ACCESS_DENIED row naming a case the actor was never shown. The attempt is
        still in the request log; what must not happen is an audit row that
        associates this counselor with that case id.
        """
        case = Case.objects.create(counselor=other_counselor, label="Not yours")
        sign_in(counselor)

        client.get(reverse("counseling:case_detail", args=[case.pk]))

        assert not AuditEvent.objects.filter(target_id=str(case.pk)).exists()
