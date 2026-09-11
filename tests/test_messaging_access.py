"""
Who may go near a conversation.

tests/test_access_matrix.py asks whether a route is reachable by an actor who is
legitimately connected to the case. This file asks the harder question — what
happens when they are not — and it is the reason the messaging app exists in the
shape it does. Three properties carry it:

  * **Spouses correspond separately.** Two counselees on one case each have their
    own threads and neither can see the other's. This is stricter than documents,
    where a counselor may deliberately share a file with the whole case: there is
    no shared setting on a thread at all, so there is nothing to get wrong.
  * **financial_admin reaches nothing.** Not a filtered list — nothing, and a
    refusal rather than an empty page, because an empty page is a claim about
    content.
  * **An admin reads and does not write.** The one page in the application with
    that shape, so it is asserted from both sides.

Everything goes through the real views and the real querysets. A test asserting on
a hand-built queryset would pass while a view called ``.objects.all()``.

Files sent with a message follow the same three properties and are covered in
tests/test_message_attachments.py, where the bytes are also the subject.
"""

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember
from apps.messaging import services
from apps.messaging.models import Message, Participant, Thread

pytestmark = pytest.mark.django_db


@pytest.fixture
def couple_case(counselor, make_user):
    """One counselor, two counselees, and a conversation belonging to each.

    The couple's case is the shape that makes isolation non-trivial, so it is the
    default here rather than a special case.
    """
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage", kind="couple")
    ada = make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")
    ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=ada)
    CaseMember.objects.create(case=case, counselee=ben)

    hers = services.start_thread(
        case=case, author=ada, subject="Something I have not said", body="I am struggling."
    )
    his = services.start_thread(case=case, author=ben, subject="My side of it", body="So am I.")
    return case, ada, ben, hers, his


class TestSpousesOnOneCase:
    """The decision that cost the most to make, and the easiest to regress."""

    def test_neither_sees_the_others_conversation_in_a_queryset(self, couple_case):
        _case, ada, ben, hers, his = couple_case

        assert list(Thread.objects.for_actor(ada)) == [hers]
        assert list(Thread.objects.for_actor(ben)) == [his]

    def test_the_others_conversation_is_a_404_not_a_403(self, client, sign_in, couple_case):
        """404, so the existence of the conversation is never confirmed.

        A 403 would tell Ada that Ben has written to their counselor about
        something, which is precisely what she must not learn from this system.
        """
        _case, ada, _ben, _hers, his = couple_case
        sign_in(ada)

        response = client.get(reverse("messaging:thread", kwargs={"pk": his.pk}))

        assert response.status_code == 404

    def test_the_others_messages_are_invisible_too(self, couple_case):
        _case, ada, _ben, hers, his = couple_case

        visible = Message.objects.for_actor(ada)

        assert set(visible.values_list("thread", flat=True)) == {hers.pk}
        assert not visible.filter(thread=his).exists()

    def test_the_case_list_shows_each_only_their_own(self, client, sign_in, couple_case):
        case, ada, _ben, hers, his = couple_case
        sign_in(ada)

        response = client.get(reverse("messaging:case_threads", kwargs={"case_pk": case.pk}))

        assert list(response.context["threads"]) == [hers]
        assert his.subject not in response.content.decode()

    def test_neither_can_be_told_how_many_they_cannot_see(self, client, sign_in, couple_case):
        """No count of the conversations withheld, for the reason the documents
        list gives none: a number is content.

        Asserted as the absence of any link to a thread that is not hers, rather
        than as the absence of a word — the page has to be readable, so a phrasing
        test would only pin the wording.
        """
        case, ada, _ben, hers, his = couple_case
        sign_in(ada)

        body = client.get(
            reverse("messaging:case_threads", kwargs={"case_pk": case.pk})
        ).content.decode()

        assert reverse("messaging:thread", kwargs={"pk": hers.pk}) in body
        assert reverse("messaging:thread", kwargs={"pk": his.pk}) not in body
        assert his.subject not in body

    def test_neither_can_reply_into_the_others_conversation(self, client, sign_in, couple_case):
        _case, ada, _ben, _hers, his = couple_case
        sign_in(ada)

        response = client.post(
            reverse("messaging:thread", kwargs={"pk": his.pk}), {"body": "I saw what you wrote."}
        )

        assert response.status_code == 404
        assert his.messages.count() == 1

    def test_the_counselor_sees_both(self, couple_case, counselor):
        _case, _ada, _ben, hers, his = couple_case

        assert set(Thread.objects.for_actor(counselor)) == {hers, his}


class TestAnotherCounselorsCase:
    def test_they_cannot_see_the_conversation_exists(self, couple_case, other_counselor):
        assert list(Thread.objects.for_actor(other_counselor)) == []

    def test_the_thread_is_a_404(self, client, sign_in, couple_case, other_counselor):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(other_counselor)

        response = client.get(reverse("messaging:thread", kwargs={"pk": hers.pk}))

        assert response.status_code == 404

    def test_the_case_list_is_a_404_because_the_case_is_invisible(
        self, client, sign_in, couple_case, other_counselor
    ):
        case, *_ = couple_case
        sign_in(other_counselor)

        response = client.get(reverse("messaging:case_threads", kwargs={"case_pk": case.pk}))

        assert response.status_code == 404


class TestFinancialAdmin:
    """Billing knows a case exists and who carries it. Not what was said on it."""

    def test_the_queryset_gives_nothing(self, couple_case, financial_admin):
        assert list(Thread.objects.for_actor(financial_admin)) == []
        assert list(Message.objects.for_actor(financial_admin)) == []
        assert list(Participant.objects.for_actor(financial_admin)) == []

    def test_the_index_refuses_rather_than_showing_an_empty_page(
        self, client, sign_in, financial_admin
    ):
        sign_in(financial_admin)

        assert client.get(reverse("messaging:index")).status_code == 403

    def test_the_case_conversations_refuse_even_though_the_case_is_visible(
        self, client, sign_in, couple_case, financial_admin
    ):
        case, *_ = couple_case
        sign_in(financial_admin)

        assert Case.objects.for_actor(financial_admin).filter(pk=case.pk).exists()
        assert (
            client.get(reverse("messaging:case_threads", kwargs={"case_pk": case.pk})).status_code
            == 403
        )

    def test_no_link_to_messaging_appears_on_a_case_they_can_see(
        self, client, sign_in, couple_case, financial_admin
    ):
        case, *_ = couple_case
        sign_in(financial_admin)

        body = client.get(
            reverse("counseling:case_detail", kwargs={"pk": case.pk})
        ).content.decode()

        assert reverse("messaging:case_threads", kwargs={"case_pk": case.pk}) not in body

    def test_the_refusal_is_audited(self, client, sign_in, financial_admin):
        sign_in(financial_admin)
        client.get(reverse("messaging:index"))

        assert AuditEvent.objects.filter(
            verb=AuditVerb.ACCESS_DENIED, actor=financial_admin
        ).exists()

    def test_they_hold_none_of_the_permissions(self, couple_case, financial_admin):
        case, ada, _ben, hers, _his = couple_case
        attachment = services.post_message(
            hers,
            author=ada,
            body="The letter I mentioned.",
            uploads=[SimpleUploadedFile("letter.pdf", b"%PDF-1.7\nprivate\ntrailer\n")],
        ).attachments.get()

        assert not financial_admin.has_perm("messaging.view_thread_index")
        assert not financial_admin.has_perm("messaging.view_case_threads", case)
        assert not financial_admin.has_perm("messaging.add_thread", case)
        assert not financial_admin.has_perm("messaging.view_thread", hers)
        assert not financial_admin.has_perm("messaging.add_message", hers)
        # The attachment permission delegates to view_thread, so this is implied by
        # the line above — asserted anyway, because "implied" is what a refactor
        # breaks quietly. tests/test_message_attachments.py covers the bytes.
        assert not financial_admin.has_perm("messaging.view_attachment", attachment)


class TestAnAdministrator:
    """Reads, and does not write. The session-note line, drawn again."""

    def test_they_can_read_the_conversation(self, client, sign_in, couple_case, admin_user):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(admin_user)

        response = client.get(reverse("messaging:thread", kwargs={"pk": hers.pk}))

        assert response.status_code == 200
        assert "I am struggling." in response.content.decode()

    def test_reading_it_is_audited(self, client, sign_in, couple_case, admin_user):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(admin_user)

        client.get(reverse("messaging:thread", kwargs={"pk": hers.pk}))

        assert AuditEvent.objects.filter(
            verb=AuditVerb.THREAD_VIEWED, actor=admin_user, target_id=str(hers.pk)
        ).exists()

    def test_they_are_not_offered_a_reply_box(self, client, sign_in, couple_case, admin_user):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(admin_user)

        response = client.get(reverse("messaging:thread", kwargs={"pk": hers.pk}))

        assert response.context["can_reply"] is False

    def test_posting_anyway_is_refused(self, client, sign_in, couple_case, admin_user):
        """The hidden form is a courtesy; this is the control."""
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(admin_user)

        response = client.post(
            reverse("messaging:thread", kwargs={"pk": hers.pk}),
            {"body": "The office needs you to reconsider."},
        )

        assert response.status_code == 403
        assert hers.messages.count() == 1

    def test_they_cannot_start_one(self, client, sign_in, couple_case, admin_user):
        case, *_ = couple_case
        sign_in(admin_user)

        response = client.post(
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            {"subject": "From the office", "body": "Please call us."},
        )

        assert response.status_code == 403
        assert Thread.objects.filter(case=case).count() == 2

    def test_they_cannot_close_or_reopen_one(self, client, sign_in, couple_case, admin_user):
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(admin_user)

        response = client.post(reverse("messaging:close", kwargs={"pk": hers.pk}))

        assert response.status_code == 403
        hers.refresh_from_db()
        assert hers.is_open

    def test_no_participant_row_is_left_behind_by_reading(
        self, client, sign_in, couple_case, admin_user
    ):
        """Reading marks nothing read, because they are not in the conversation.

        If an administrator's visit created a participant row it would appear in
        the thread's audience — and the counselee would be shown a third name on
        a conversation they were told was private.
        """
        _case, _ada, _ben, hers, _his = couple_case
        sign_in(admin_user)

        client.get(reverse("messaging:thread", kwargs={"pk": hers.pk}))

        assert not hers.participants.filter(user=admin_user).exists()
        assert hers.participants.count() == 2


class TestACounseleeWhoseMembershipEnded:
    def test_they_stop_seeing_their_own_correspondence(self, couple_case):
        """The same way round as documents: the record stays with the case.

        Participation alone is not enough — the queryset requires a current
        membership too, which is what ends the access rather than only the
        counseling.
        """
        case, ada, _ben, hers, _his = couple_case
        CaseMember.objects.filter(case=case, counselee=ada).update(ended_on=org_today())

        assert list(Thread.objects.for_actor(ada)) == []
        assert hers.participants.filter(user=ada).exists()

    def test_the_counselor_still_has_it(self, couple_case, counselor):
        case, ada, _ben, hers, _his = couple_case
        CaseMember.objects.filter(case=case, counselee=ada).update(ended_on=org_today())

        assert hers in Thread.objects.for_actor(counselor)

    def test_they_cannot_reply_after_it_ends(self, client, sign_in, couple_case):
        case, ada, _ben, hers, _his = couple_case
        CaseMember.objects.filter(case=case, counselee=ada).update(ended_on=org_today())
        sign_in(ada)

        response = client.post(
            reverse("messaging:thread", kwargs={"pk": hers.pk}), {"body": "One last thing."}
        )

        assert response.status_code == 404
        assert hers.messages.count() == 1


class TestACaseThatChangedCounselor:
    def test_the_new_counselor_inherits_the_correspondence(self, couple_case, other_counselor):
        """Scoping is by case, not by participation, so a reassignment carries the
        history — the same rule documents follow."""
        case, _ada, _ben, hers, his = couple_case
        case.counselor = other_counselor
        case.save(update_fields=["counselor", "updated_at"])

        assert set(Thread.objects.for_actor(other_counselor)) == {hers, his}

    def test_the_previous_counselor_loses_it(self, couple_case, counselor, other_counselor):
        case, *_ = couple_case
        case.counselor = other_counselor
        case.save(update_fields=["counselor", "updated_at"])

        assert list(Thread.objects.for_actor(counselor)) == []

    def test_replying_gives_them_a_place_in_the_conversation(
        self, couple_case, other_counselor, counselor
    ):
        """Not a widening: they could already read it. The row is what gives them
        an unread count, and it replaces nobody — the previous counselor's row
        stays, because they wrote some of what is there."""
        case, ada, _ben, hers, _his = couple_case
        case.counselor = other_counselor
        case.save(update_fields=["counselor", "updated_at"])

        services.post_message(hers, author=other_counselor, body="I have taken over your case.")

        assert set(hers.participants.values_list("user", flat=True)) == {
            ada.pk,
            counselor.pk,
            other_counselor.pk,
        }

    def test_the_counselee_is_still_the_only_counselee_in_it(
        self, couple_case, other_counselor, counselor
    ):
        case, ada, ben, hers, _his = couple_case
        case.counselor = other_counselor
        case.save(update_fields=["counselor", "updated_at"])
        services.post_message(hers, author=other_counselor, body="I have taken over your case.")

        assert not hers.participants.filter(user=ben).exists()
        assert list(Thread.objects.for_actor(ben)) != [hers]
        assert ada.pk in set(hers.participants.values_list("user", flat=True))


class TestAnonymous:
    def test_every_route_sends_them_to_sign_in(self, client, couple_case):
        case, _ada, _ben, hers, _his = couple_case

        for url in (
            reverse("messaging:index"),
            reverse("messaging:case_threads", kwargs={"case_pk": case.pk}),
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            reverse("messaging:thread", kwargs={"pk": hers.pk}),
        ):
            response = client.get(url)
            assert response.status_code == 302, url
            assert "/login/" in response["Location"], url

    def test_nothing_is_emailed_by_a_refused_request(self, client, couple_case):
        case, *_ = couple_case
        mail.outbox.clear()

        client.post(
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            {"subject": "Hello", "body": "Something."},
        )

        assert mail.outbox == []
        assert Thread.objects.filter(case=case).count() == 2
