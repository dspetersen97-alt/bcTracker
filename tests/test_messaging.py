"""
What messaging does. tests/test_messaging_access.py covers who may do it.

The service layer is the subject here, because that is where the awkward parts
live: the audience of a thread is written once and never widened, a message cannot
be rewritten afterwards, and an email says that something is waiting without
saying anything about what.
"""

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember, CaseStatus
from apps.messaging import services
from apps.messaging.models import MESSAGE_MAX_CHARACTERS, Message, Participant, Thread

pytestmark = pytest.mark.django_db


@pytest.fixture
def case(counselor, counselee):
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    CaseMember.objects.create(case=case, counselee=counselee)
    return case


@pytest.fixture
def thread(case, counselee):
    return services.start_thread(
        case=case,
        author=counselee,
        subject="A question about the homework",
        body="Could we go over the second page?",
    )


class TestStartingAConversation:
    def test_the_audience_is_the_counselor_and_one_counselee(self, case, counselee, counselor):
        thread = services.start_thread(
            case=case, author=counselee, subject="Hello", body="A first message."
        )

        assert set(thread.participants.values_list("user", flat=True)) == {
            counselee.pk,
            counselor.pk,
        }

    def test_a_counselor_must_say_who_it_is_with(self, case, counselor):
        with pytest.raises(services.MessagingError):
            services.start_thread(case=case, author=counselor, subject="Hello", body="Something.")

    def test_a_counselor_cannot_write_to_someone_who_is_not_on_the_case(
        self, case, counselor, make_user
    ):
        stranger = make_user(Role.COUNSELEE)

        with pytest.raises(services.MessagingError):
            services.start_thread(
                case=case,
                author=counselor,
                counselee=stranger,
                subject="Hello",
                body="Something.",
            )

    def test_a_counselee_naming_somebody_else_is_refused_rather_than_ignored(
        self, case, counselee, make_user
    ):
        """The refusal matters more than the outcome.

        Quietly substituting the author would also be safe, but it would mean a
        request built by hand had been accepted rather than rejected, and the next
        person to read the code could not tell which.
        """
        other = make_user(Role.COUNSELEE)
        CaseMember.objects.create(case=case, counselee=other)

        with pytest.raises(services.MessagingError):
            services.start_thread(
                case=case, author=counselee, counselee=other, subject="Hi", body="Something."
            )

    def test_an_admin_cannot_start_one_even_through_the_service(self, case, admin_user):
        with pytest.raises(services.MessagingError):
            services.start_thread(case=case, author=admin_user, subject="Hello", body="Something.")

    def test_the_first_message_is_the_body(self, case, counselee):
        thread = services.start_thread(
            case=case, author=counselee, subject="Hello", body="  A first message.  "
        )

        message = thread.messages.get()
        assert message.body == "A first message."
        assert message.author == counselee

    def test_an_empty_message_is_refused(self, case, counselee):
        with pytest.raises(services.MessagingError):
            services.start_thread(case=case, author=counselee, subject="Hello", body="   ")

    def test_a_subject_is_required(self, case, counselee):
        with pytest.raises(services.MessagingError):
            services.start_thread(case=case, author=counselee, subject="  ", body="Something.")

    def test_it_is_audited_without_the_words(self, case, counselee, counselor):
        services.start_thread(
            case=case, author=counselee, subject="Hello", body="Something private."
        )

        event = AuditEvent.objects.get(verb=AuditVerb.THREAD_STARTED)
        assert event.actor == counselee
        assert event.case_id_snapshot == str(case.pk)
        assert event.metadata["characters"] == len("Something private.")
        assert "Something private." not in str(event.metadata)


class TestReplying:
    def test_a_reply_moves_the_threads_clock(self, thread, counselor):
        before = thread.last_message_at

        services.post_message(thread, author=counselor, body="Yes, let's.")

        thread.refresh_from_db()
        assert thread.last_message_at > before
        assert thread.messages.count() == 2

    def test_a_message_is_too_long_to_be_a_document(self, thread, counselor):
        with pytest.raises(services.MessagingError):
            services.post_message(thread, author=counselor, body="x" * (MESSAGE_MAX_CHARACTERS + 1))

    def test_a_closed_conversation_takes_no_more(self, thread, counselor):
        services.close_thread(thread, actor=counselor)

        with pytest.raises(services.MessagingError):
            services.post_message(thread, author=counselor, body="One more thing.")

    def test_a_sent_message_cannot_be_rewritten(self, thread):
        message = thread.messages.get()
        message.body = "Something else entirely."

        with pytest.raises(ValueError):
            message.save()

        message.refresh_from_db()
        assert message.body == "Could we go over the second page?"

    def test_nobody_has_permission_to_change_or_delete_one(self, thread, counselor, admin_user):
        message = thread.messages.get()

        for actor in (counselor, admin_user, message.author):
            assert not actor.has_perm("messaging.change_message", message)
            assert not actor.has_perm("messaging.delete_message", message)

    def test_it_is_audited_without_the_words(self, thread, counselor):
        services.post_message(thread, author=counselor, body="Yes, of course.")

        event = AuditEvent.objects.get(verb=AuditVerb.MESSAGE_SENT)
        assert event.actor == counselor
        assert event.metadata["characters"] == len("Yes, of course.")
        assert "of course" not in str(event.metadata)


class TestClosingAndReopening:
    def test_closing_deletes_nothing(self, thread, counselor):
        services.close_thread(thread, actor=counselor)

        thread.refresh_from_db()
        assert not thread.is_open
        assert thread.messages.count() == 1

    def test_closing_twice_is_not_an_error(self, thread, counselor):
        services.close_thread(thread, actor=counselor)
        closed_at = Thread.objects.get(pk=thread.pk).closed_at

        services.close_thread(thread, actor=counselor)

        assert Thread.objects.get(pk=thread.pk).closed_at == closed_at

    def test_reopening_lets_the_conversation_continue(self, thread, counselor):
        services.close_thread(thread, actor=counselor)
        services.reopen_thread(thread, actor=counselor)

        services.post_message(thread, author=counselor, body="One more thing.")

        assert thread.messages.count() == 2

    def test_both_are_audited(self, thread, counselor):
        services.close_thread(thread, actor=counselor)
        services.reopen_thread(thread, actor=counselor)

        assert AuditEvent.objects.filter(verb=AuditVerb.THREAD_CLOSED).count() == 1
        assert AuditEvent.objects.filter(verb=AuditVerb.THREAD_REOPENED).count() == 1


class TestWhatIsUnread:
    def test_the_author_never_owes_themselves_a_reply(self, thread, counselee):
        assert services.unread_total(counselee) == 0

    def test_the_other_party_is_told_there_is_something(self, thread, counselor):
        assert services.unread_total(counselor) == 1

    def test_reading_it_clears_the_count(self, thread, counselor):
        services.mark_read(thread, user=counselor)

        assert services.unread_total(counselor) == 0

    def test_a_later_message_counts_again(self, thread, counselor, counselee):
        services.mark_read(thread, user=counselor)
        services.post_message(thread, author=counselee, body="Also this.")

        assert services.unread_total(counselor) == 1

    def test_a_counselee_on_two_cases_counts_both(self, counselee, make_user):
        for label in ("First", "Second"):
            counselor = make_user(Role.COUNSELOR)
            case = Case.objects.create(counselor=counselor, label=label)
            CaseMember.objects.create(case=case, counselee=counselee)
            services.start_thread(
                case=case,
                author=counselor,
                counselee=counselee,
                subject=label,
                body="Something to read.",
            )

        assert services.unread_total(counselee) == 2

    def test_a_thread_nobody_has_opened_is_entirely_unread(self, case, counselee, counselor):
        """The NULL case, which a naive timestamp comparison gets wrong.

        ``last_read_at`` is null until somebody opens the conversation, and
        ``created_at > NULL`` is NULL rather than true — so without the coalesce in
        ``unread_for`` a brand new thread would report nothing waiting.
        """
        thread = services.start_thread(case=case, author=counselee, subject="Hello", body="One.")
        services.post_message(thread, author=counselee, body="Two.")

        assert Participant.objects.get(thread=thread, user=counselor).last_read_at is None
        assert services.unread_total(counselor) == 2

    def test_an_inherited_case_shows_its_correspondence_as_unread(
        self, case, thread, other_counselor
    ):
        """A counselor who has just been given the case has no participant row.

        They can read the thread — scoping is by case, not by participation — and
        the count has to treat all of it as unread rather than as read.
        """
        case.counselor = other_counselor
        case.save(update_fields=["counselor", "updated_at"])

        assert not thread.participants.filter(user=other_counselor).exists()
        assert services.unread_total(other_counselor) == 1

    def test_the_roles_with_no_place_in_a_conversation_get_zero(
        self, thread, admin_user, financial_admin
    ):
        """Not a filtered count — zero, without asking the question.

        An admin can read every thread in the ministry, so counting what they have
        not opened would put a number in the hundreds in the header and mean
        nothing by it.
        """
        assert services.unread_total(admin_user) == 0
        assert services.unread_total(financial_admin) == 0

    def test_the_per_thread_counts_agree_with_the_total(self, thread, counselor, counselee):
        services.post_message(thread, author=counselee, body="Also this.")

        counts = services.unread_counts_by_thread(counselor)

        assert counts == {thread.pk: 2}
        assert sum(counts.values()) == services.unread_total(counselor)


class TestTheHeaderCount:
    """The badge in base.html, which every page inherits from the context processor.

    Asserted on the account page rather than a dashboard because that one route
    renders for all four roles, which is the point of a header.
    """

    def test_a_counselee_is_told_on_every_page(self, client, sign_in, thread, counselee):
        services.post_message(thread, author=thread.case.counselor, body="Have a look at this.")
        sign_in(counselee)

        response = client.get(reverse("accounts:home"))

        assert response.context["unread_messages"] == 1
        assert reverse("messaging:index") in response.content.decode()

    def test_it_is_absent_for_a_role_with_no_conversations(
        self, client, sign_in, thread, financial_admin
    ):
        sign_in(financial_admin)

        response = client.get(reverse("accounts:home"))

        assert response.context["unread_messages"] == 0
        assert reverse("messaging:index") not in response.content.decode()


class TestNotifications:
    def test_the_other_party_is_emailed(self, case, counselee, counselor):
        mail.outbox.clear()

        services.start_thread(
            case=case, author=counselee, subject="Hello", body="Something private."
        )

        assert [message.to for message in mail.outbox] == [[counselor.email]]

    def test_the_email_says_nothing_about_the_counseling(self, case, counselee, counselor):
        mail.outbox.clear()

        services.start_thread(
            case=case,
            author=counselee,
            subject="The argument on Sunday",
            body="I said something I regret.",
        )

        sent = mail.outbox[0]
        for secret in ("argument", "Sunday", "regret", case.label, "Ashford"):
            assert secret not in sent.subject
            assert secret not in sent.body

    def test_the_author_is_not_emailed_their_own_message(self, thread, counselee, counselor):
        mail.outbox.clear()

        services.post_message(thread, author=counselor, body="Yes, of course.")

        assert [message.to for message in mail.outbox] == [[counselee.email]]

    def test_a_second_message_does_not_send_a_second_email(self, thread, counselor):
        """One nudge per conversation until it is read.

        A counselor writing three paragraphs as three messages should produce one
        email, not three.
        """
        mail.outbox.clear()

        services.post_message(thread, author=counselor, body="One.")
        services.post_message(thread, author=counselor, body="Two.")

        assert len(mail.outbox) == 1

    def test_a_message_after_a_read_does_send_one(self, thread, counselor, counselee):
        services.post_message(thread, author=counselor, body="One.")
        services.mark_read(thread, user=counselee)
        mail.outbox.clear()

        services.post_message(thread, author=counselor, body="Two.")

        assert len(mail.outbox) == 1

    def test_a_deactivated_account_is_not_written_to(self, thread, counselor, counselee):
        counselee.is_active = False
        counselee.save(update_fields=["is_active"])
        mail.outbox.clear()

        services.post_message(thread, author=counselor, body="Are you there?")

        assert mail.outbox == []

    def test_the_link_comes_from_the_configured_base_url(self, settings, thread, counselor):
        settings.SITE_BASE_URL = "https://counseling.example.org"
        mail.outbox.clear()

        services.post_message(thread, author=counselor, body="Have a look.")

        assert f"https://counseling.example.org/messages/{thread.pk}/" in mail.outbox[0].body

    def test_a_failed_send_does_not_lose_the_message(self, settings, thread, counselor):
        settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
        settings.EMAIL_HOST = "127.0.0.1"
        settings.EMAIL_PORT = 1

        message = services.post_message(thread, author=counselor, body="Still stored.")

        assert Message.objects.filter(pk=message.pk).exists()


class TestThroughTheViews:
    def test_a_counselee_starts_a_conversation_and_gets_an_answer(
        self, client, sign_in, case, counselee, counselor
    ):
        sign_in(counselee)

        started = client.post(
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            {"subject": "A question", "body": "Could we go over page two?"},
        )
        assert started.status_code == 302
        thread = Thread.objects.get()

        client.post(reverse("accounts:logout"))
        sign_in(counselor)
        replied = client.post(
            reverse("messaging:thread", kwargs={"pk": thread.pk}),
            {"body": "Yes, bring it with you."},
        )

        assert replied.status_code == 302
        assert [message.body for message in thread.messages.all()] == [
            "Could we go over page two?",
            "Yes, bring it with you.",
        ]

    def test_a_counselor_chooses_who_it_is_with(self, client, sign_in, case, counselor, make_user):
        spouse = make_user(Role.COUNSELEE)
        CaseMember.objects.create(case=case, counselee=spouse)
        sign_in(counselor)

        response = client.post(
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            {"counselee": spouse.pk, "subject": "Homework", "body": "Here is this week's."},
        )

        assert response.status_code == 302
        thread = Thread.objects.get()
        assert set(thread.participants.values_list("user", flat=True)) == {
            spouse.pk,
            counselor.pk,
        }

    def test_the_counselee_field_is_not_offered_to_a_counselee(
        self, client, sign_in, case, counselee
    ):
        sign_in(counselee)

        response = client.get(reverse("messaging:start", kwargs={"case_pk": case.pk}))

        assert "counselee" not in response.context["form"].fields

    def test_reading_a_conversation_is_audited_and_marks_it_read(
        self, client, sign_in, thread, counselor
    ):
        sign_in(counselor)

        client.get(reverse("messaging:thread", kwargs={"pk": thread.pk}))

        assert AuditEvent.objects.filter(
            verb=AuditVerb.THREAD_VIEWED, actor=counselor, target_id=str(thread.pk)
        ).exists()
        assert services.unread_total(counselor) == 0

    def test_an_empty_reply_is_a_form_error_not_a_message(self, client, sign_in, thread, counselor):
        sign_in(counselor)

        response = client.post(reverse("messaging:thread", kwargs={"pk": thread.pk}), {"body": " "})

        assert response.status_code == 200
        assert response.context["form"].errors
        assert thread.messages.count() == 1

    def test_a_closed_case_takes_no_new_conversations(self, client, sign_in, case, counselee):
        case.close(on=org_today())
        assert case.status == CaseStatus.CLOSED
        sign_in(counselee)

        response = client.get(reverse("messaging:start", kwargs={"case_pk": case.pk}))

        assert response.status_code == 403

    def test_a_closed_case_takes_no_new_messages_either(
        self, client, sign_in, thread, case, counselor
    ):
        case.close(on=org_today())
        sign_in(counselor)

        response = client.post(
            reverse("messaging:thread", kwargs={"pk": thread.pk}), {"body": "One more thing."}
        )

        assert response.status_code == 403
        assert thread.messages.count() == 1
