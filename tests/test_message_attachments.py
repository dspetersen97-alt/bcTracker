"""
Sending a file with a message, and getting it back out again.

tests/test_messaging_access.py covers who may read a conversation. This file
covers the bytes, and it exists because an attachment is the one thing in
messaging that leaves the database — so it is the one thing that can be wrong in
two places at once.

Three properties:

  * **A file is reachable by exactly the people who can read the message it came
    with.** Not "the case", not "the uploader and the counselor" — the
    conversation. On a couple's case that is the whole difference between this and
    a Document, and it is asserted from the queryset and through the view.
  * **Words and files arrive together or not at all.** A file that cannot be
    scanned, sealed, or stored takes the message down with it, because a sender who
    is told "sent" and whose attachment silently vanished has been lied to about
    something they cannot check.
  * **Nothing is stored in the clear and nothing is served unrecorded.** The same
    two guarantees documents makes, asserted again here rather than assumed, since
    this is a second call site for the same pipeline.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.core.dates import org_today
from apps.counseling.models import Case, CaseMember
from apps.documents import ingest, scanning, storage
from apps.messaging import services
from apps.messaging.models import (
    MESSAGE_MAX_ATTACHMENTS,
    Message,
    MessageAttachment,
    Thread,
)
from tests.conftest import jpeg_bytes

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\nwhat I could not say out loud\ntrailer\n"


def a_file(name="letter.pdf", data=PDF):
    return SimpleUploadedFile(name, data)


@pytest.fixture
def couple_case(counselor, make_user):
    """One counselor and two counselees, as in the messaging access tests."""
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage", kind="couple")
    ada = make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")
    ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=ada)
    CaseMember.objects.create(case=case, counselee=ben)
    return case, ada, ben


@pytest.fixture
def send(couple_case):
    """Start a conversation carrying files, through the real service.

    Returns the thread and its attachments, because almost every test here needs
    both and fetching the second from the first is noise.
    """
    case, ada, ben = couple_case

    def _send(author=None, *, uploads=None, subject="A letter", body="Please read this."):
        # ``None`` rather than a default list, because an UploadedFile is read
        # once: a default built at definition time would be exhausted by the
        # second test to use it.
        thread = services.start_thread(
            case=case,
            author=author if author is not None else ada,
            subject=subject,
            body=body,
            uploads=[a_file()] if uploads is None else list(uploads),
        )
        return thread, list(MessageAttachment.objects.filter(message__thread=thread))

    return _send


def downloaded(client, attachment):
    response = client.get(reverse("messaging:attachment", args=[attachment.pk]))
    if response.status_code != 200:
        return response, b""
    return response, b"".join(response.streaming_content)


class TestSendingAFile:
    def test_a_file_sent_with_a_message_is_stored_and_recorded(self, send, couple_case):
        _case, ada, _ben = couple_case

        thread, attachments = send(ada, uploads=[a_file("letter.pdf")])

        attachment = attachments[0]
        assert attachment.original_filename == "letter.pdf"
        assert attachment.byte_size == len(PDF)
        assert attachment.message.author == ada
        assert attachment.message.thread == thread
        event = AuditEvent.objects.get(verb=AuditVerb.MESSAGE_ATTACHMENT_ADDED)
        assert event.actor == ada
        assert event.metadata["filename"] == "letter.pdf"
        assert event.case_id_snapshot == str(thread.case_id)

    def test_the_message_records_how_many_files_came_with_it_and_not_what_is_in_them(self, send):
        """A count is metadata; a filename in the *thread* row would be content.

        The count is here because "she sent three things and I received two" is a
        question the trail should be able to answer.
        """
        send(uploads=[a_file("one.pdf"), a_file("two.pdf")])

        started = AuditEvent.objects.get(verb=AuditVerb.THREAD_STARTED)
        assert started.metadata["attachments"] == 2
        assert "what I could not say" not in str(started.metadata)

    def test_a_reply_can_carry_a_file_too(self, send, couple_case):
        _case, ada, _ben = couple_case
        thread, _ = send(ada, uploads=[])

        message = services.post_message(
            thread, author=ada, body="Here it is.", uploads=[a_file("late.pdf")]
        )

        assert [file.original_filename for file in message.attachments.all()] == ["late.pdf"]
        assert (
            AuditEvent.objects.filter(verb=AuditVerb.MESSAGE_SENT).get().metadata["attachments"]
            == 1
        )

    def test_what_lands_on_disk_is_not_what_was_sent(self, send, _document_store):
        """The whole point of the store, asserted at the store.

        Every other test in this file would pass if the bytes were written
        straight through.
        """
        send(uploads=[a_file()])

        blobs = [path for path in _document_store.rglob("*") if path.is_file()]
        assert blobs, "something should have been written"
        for path in blobs:
            assert b"could not say" not in path.read_bytes()

    def test_a_photo_loses_its_location_on_the_way_in(self, send, counselor, client, sign_in):
        """The strip is wired up on this path too, not only on the documents one.

        A photo of a bruise or a room is exactly the file this matters for, and
        messaging is the easier way to send one.
        """
        from PIL import Image
        from PIL.TiffImagePlugin import IFDRational

        exif = Image.Exif()
        gps = exif.get_ifd(0x8825)
        gps[1] = "N"
        gps[2] = (IFDRational(40), IFDRational(44), IFDRational(54, 100))
        _thread, attachments = send(
            uploads=[a_file("room.jpg", jpeg_bytes(size=(32, 24), exif=exif))]
        )
        sign_in(counselor)

        _response, body = downloaded(client, attachments[0])

        assert not Image.open(io.BytesIO(body)).getexif().get_ifd(0x8825)

    def test_more_files_than_the_limit_is_a_refusal_and_writes_nothing(
        self, couple_case, send, _document_store
    ):
        _case, ada, _ben = couple_case

        with pytest.raises(services.MessagingError):
            send(
                ada,
                uploads=[a_file(f"{index}.pdf") for index in range(MESSAGE_MAX_ATTACHMENTS + 1)],
            )

        assert not Thread.objects.exists()
        assert not list(_document_store.rglob("*"))

    def test_an_empty_file_field_sends_an_ordinary_message(self, send):
        """A form always posts the field. An absent file is not an error."""
        thread, attachments = send(uploads=[None, ""])

        assert attachments == []
        assert thread.messages.count() == 1


class TestAFileThatCannotBeAccepted:
    def test_an_infected_file_takes_the_whole_message_with_it(
        self, couple_case, send, monkeypatch, _document_store
    ):
        """Not "message sent, attachment rejected".

        Half-delivering correspondence is worse than refusing it: the sender's
        words go on to be answered while the thing they were about is missing, and
        only the recipient can tell.
        """

        def infected(upload):
            raise scanning.InfectedFile("Win.Test.EICAR_HDB-1")

        monkeypatch.setattr(ingest.scanning, "scan", infected)
        _case, ada, _ben = couple_case

        with pytest.raises(services.MessagingError):
            send(ada, uploads=[a_file()])

        assert not Thread.objects.exists()
        assert not Message.objects.exists()
        assert not MessageAttachment.objects.exists()
        assert not list(_document_store.rglob("*"))

    def test_an_infected_file_is_recorded_against_the_case(self, couple_case, send, monkeypatch):
        """Recorded, not merely refused — and recorded before the rollback.

        The rejection is the security event. It happens outside the transaction
        that writes the message precisely so that rolling that transaction back
        cannot erase it.
        """

        def infected(upload):
            raise scanning.InfectedFile("Win.Test.EICAR_HDB-1")

        monkeypatch.setattr(ingest.scanning, "scan", infected)
        case, ada, _ben = couple_case

        with pytest.raises(services.MessagingError):
            send(ada, uploads=[a_file("invoice.pdf")])

        event = AuditEvent.objects.get(verb=AuditVerb.MESSAGE_ATTACHMENT_REJECTED)
        assert event.actor == ada
        assert event.metadata["signature"] == "Win.Test.EICAR_HDB-1"
        assert event.metadata["filename"] == "invoice.pdf"
        assert event.case_id_snapshot == str(case.pk)

    def test_a_scanner_that_is_down_is_not_reported_as_a_bad_file(
        self, couple_case, send, monkeypatch, _document_store
    ):
        """Fail-closed and honest about why.

        Telling a counselee their file was rejected would send them away to try a
        different one while nobody looks at the actual outage.
        """

        def unavailable(upload):
            raise scanning.ScannerUnavailable("connection refused")

        monkeypatch.setattr(ingest.scanning, "scan", unavailable)
        _case, ada, _ben = couple_case

        with pytest.raises(scanning.ScannerUnavailable):
            send(ada, uploads=[a_file()])

        assert not Thread.objects.exists()
        assert not list(_document_store.rglob("*"))

    def test_a_file_over_the_size_limit_is_refused(self, couple_case, send, settings):
        settings.DOCUMENT_MAX_BYTES = 100
        _case, ada, _ben = couple_case

        with pytest.raises(services.MessagingError):
            send(ada, uploads=[a_file(data=b"%PDF-1.7\n" + b"x" * 500)])

        assert not Thread.objects.exists()

    def test_a_failure_half_way_through_leaves_no_ciphertext_behind(
        self, couple_case, send, monkeypatch, _document_store
    ):
        """Two files, and the second one cannot be saved.

        The transaction takes the message and the first attachment row back, but a
        database rollback knows nothing about the volume — so the service tracks
        what it wrote and removes it. Without that, a failed send would leave an
        orphaned blob that nothing will ever read or delete.
        """
        real_save = MessageAttachment.save
        calls = []

        def save_once_then_fail(self, *args, **kwargs):
            calls.append(self)
            if len(calls) > 1:
                raise RuntimeError("the database went away")
            return real_save(self, *args, **kwargs)

        monkeypatch.setattr(MessageAttachment, "save", save_once_then_fail)
        _case, ada, _ben = couple_case

        with pytest.raises(RuntimeError):
            send(ada, uploads=[a_file("one.pdf"), a_file("two.pdf")])

        assert not Thread.objects.exists()
        assert not MessageAttachment.objects.exists()
        assert not [path for path in _document_store.rglob("*") if path.is_file()]


class TestWhoMayOpenAFile:
    """The row in the access matrix, taken apart.

    Every assertion here is really the same one: access follows the thread. What
    varies is who is asking.
    """

    def test_the_recipient_gets_the_file_back_byte_for_byte(
        self, couple_case, send, client, sign_in
    ):
        _case, ada, _ben = couple_case
        counselor = _case.counselor
        _thread, attachments = send(ada)
        sign_in(counselor)

        response, body = downloaded(client, attachments[0])

        assert response.status_code == 200
        assert body == PDF

    def test_the_sender_can_open_their_own_again(self, couple_case, send, client, sign_in):
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada)
        sign_in(ada)

        response, body = downloaded(client, attachments[0])

        assert response.status_code == 200
        assert body == PDF

    def test_the_spouse_cannot_see_it_in_a_queryset(self, couple_case, send):
        _case, ada, ben = couple_case
        _thread, attachments = send(ada)

        assert list(MessageAttachment.objects.for_actor(ada)) == attachments
        assert list(MessageAttachment.objects.for_actor(ben)) == []

    def test_the_spouse_asking_for_it_by_id_gets_a_404(self, couple_case, send, client, sign_in):
        """404, so the file's existence is never confirmed.

        Ben guessing ids must not be able to learn that Ada sent their counselor
        something, which is what a 403 would tell him.
        """
        _case, ada, ben = couple_case
        _thread, attachments = send(ada)
        sign_in(ben)

        response, _body = downloaded(client, attachments[0])

        assert response.status_code == 404

    def test_another_counselors_counselee_gets_a_404(
        self, couple_case, send, make_user, client, sign_in
    ):
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada)
        stranger = make_user(Role.COUNSELEE)
        sign_in(stranger)

        response, _body = downloaded(client, attachments[0])

        assert response.status_code == 404

    def test_financial_admin_gets_a_404(self, couple_case, send, financial_admin, client, sign_in):
        """Billing sees the case and cannot see a file sent on it.

        404 rather than 403 because this route is keyed by the attachment, and as
        far as financial_admin is concerned the attachment does not exist.
        """
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada)
        sign_in(financial_admin)

        response, _body = downloaded(client, attachments[0])

        assert response.status_code == 404
        assert list(MessageAttachment.objects.for_actor(financial_admin)) == []

    def test_an_administrator_may_open_it_and_it_is_recorded_as_them(
        self, couple_case, send, admin_user, client, sign_in
    ):
        """The same read-only oversight they have over the message itself.

        Worth asserting rather than assuming: an administrator downloading a
        counselee's file is the broadest read this route permits, so the audit row
        naming them is the control that makes it reviewable.
        """
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada)
        sign_in(admin_user)

        response, body = downloaded(client, attachments[0])

        assert response.status_code == 200
        assert body == PDF
        event = AuditEvent.objects.get(verb=AuditVerb.MESSAGE_ATTACHMENT_DOWNLOADED)
        assert event.actor == admin_user

    def test_anonymous_is_sent_to_the_login_page(self, couple_case, send, client):
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada)

        response = client.get(reverse("messaging:attachment", args=[attachments[0].pk]))

        assert response.status_code == 302
        assert "/login/" in response["Location"]

    def test_a_counselee_whose_membership_ended_loses_the_file_too(
        self, couple_case, send, client, sign_in
    ):
        """Consistent with the conversation it came with, which they also lose.

        A file that stayed readable after the case ended would be an access path
        that outlived the relationship it was granted for.
        """
        case, ada, _ben = couple_case
        _thread, attachments = send(ada)
        case.members.filter(counselee=ada).update(ended_on=org_today())
        sign_in(ada)

        response, _body = downloaded(client, attachments[0])

        assert response.status_code == 404

    def test_a_closed_conversation_still_gives_its_files_back(
        self, couple_case, send, client, sign_in
    ):
        """Closing ends the correspondence, not the record of it.

        The counselee cannot reply any more; nothing they sent has been taken away.
        """
        case, ada, _ben = couple_case
        thread, attachments = send(ada)
        services.close_thread(thread, actor=case.counselor)
        sign_in(ada)

        response, body = downloaded(client, attachments[0])

        assert response.status_code == 200
        assert body == PDF


class TestTheDownloadItself:
    def test_it_is_always_an_attachment_and_never_sniffed(self, couple_case, send, client, sign_in):
        """Even for an image, which is the case that tempts inline rendering.

        An HTML or SVG file served inline from our own origin would run with the
        session cookie of whoever opened it.
        """
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada, uploads=[a_file("room.jpg", jpeg_bytes())])
        sign_in(ada)

        response, _body = downloaded(client, attachments[0])

        assert response["Content-Disposition"].startswith("attachment;")
        assert "room.jpg" in response["Content-Disposition"]
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Cache-Control"] == "no-store, max-age=0"
        assert response["Referrer-Policy"] == "no-referrer"

    def test_the_content_type_is_the_one_we_sniffed_not_the_one_we_were_told(
        self, couple_case, client, sign_in
    ):
        """A client-supplied type is an instruction about how to execute the reply."""
        case, ada, _ben = couple_case
        lying = SimpleUploadedFile("letter.pdf", PDF, content_type="text/html")
        thread = services.start_thread(
            case=case, author=ada, subject="A letter", body="Please read this.", uploads=[lying]
        )
        attachment = MessageAttachment.objects.get(message__thread=thread)
        sign_in(ada)

        response, _body = downloaded(client, attachment)

        assert attachment.content_type == "application/pdf"
        assert response["Content-Type"] == "application/pdf"

    def test_an_unrecordable_download_serves_nothing(
        self, couple_case, send, client, sign_in, monkeypatch
    ):
        """The one place the audit trail is allowed to break the user's request.

        An unlogged disclosure is what the trail exists to prevent, so a trail that
        cannot be written costs the download. Everywhere else ``record`` swallows.
        """

        def broken(*args, **kwargs):
            raise RuntimeError("the audit table is unavailable")

        monkeypatch.setattr(services, "record_or_raise", broken)
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada)
        sign_in(ada)

        with pytest.raises(RuntimeError):
            client.get(reverse("messaging:attachment", args=[attachments[0].pk]))

    def test_a_missing_blob_is_a_404_rather_than_a_crash(self, couple_case, send, client, sign_in):
        """What a restore that missed the documents volume looks like."""
        _case, ada, _ben = couple_case
        _thread, attachments = send(ada)
        storage.delete_blob(attachments[0].storage_key)
        sign_in(ada)

        response, _body = downloaded(client, attachments[0])

        assert response.status_code == 404


class TestThroughTheBrowser:
    """The form and the page, since a service nobody can reach sends no files."""

    def test_a_counselee_can_start_a_conversation_with_a_file_attached(
        self, couple_case, client, sign_in
    ):
        case, ada, _ben = couple_case
        sign_in(ada)

        client.post(
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            {
                "subject": "A letter",
                "body": "Please read this before Thursday.",
                "files": [a_file("letter.pdf")],
            },
        )

        attachment = MessageAttachment.objects.get()
        assert attachment.original_filename == "letter.pdf"
        assert attachment.message.author == ada

    def test_several_files_at_once_all_arrive(self, couple_case, client, sign_in):
        """The reason MultipleFileField exists: without it only the last one lands."""
        case, ada, _ben = couple_case
        sign_in(ada)

        client.post(
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            {
                "subject": "Three things",
                "body": "All of these.",
                "files": [a_file("one.pdf"), a_file("two.pdf"), a_file("three.pdf")],
            },
        )

        assert sorted(MessageAttachment.objects.values_list("original_filename", flat=True)) == [
            "one.pdf",
            "three.pdf",
            "two.pdf",
        ]

    def test_a_rejected_file_comes_back_as_a_form_error_with_the_words_intact(
        self, couple_case, client, sign_in, monkeypatch
    ):
        """The sender still has what they wrote.

        A stack trace over lost words is the difference between a refusal somebody
        can act on and one that makes them retype a difficult letter.
        """

        def infected(upload):
            raise scanning.InfectedFile("Win.Test.EICAR_HDB-1")

        monkeypatch.setattr(ingest.scanning, "scan", infected)
        case, ada, _ben = couple_case
        sign_in(ada)

        response = client.post(
            reverse("messaging:start", kwargs={"case_pk": case.pk}),
            {"subject": "A letter", "body": "Please read this.", "files": [a_file()]},
        )

        assert response.status_code == 200
        assert response.context["form"].errors
        assert "Please read this." in response.context["form"]["body"].value()
        assert not Thread.objects.exists()

    def test_a_reply_can_attach_a_file(self, couple_case, send, client, sign_in):
        _case, ada, _ben = couple_case
        thread, _ = send(ada, uploads=[])
        sign_in(ada)

        client.post(
            reverse("messaging:thread", kwargs={"pk": thread.pk}),
            {"body": "Here is the page.", "files": [a_file("page.pdf")]},
        )

        assert MessageAttachment.objects.get().original_filename == "page.pdf"

    def test_the_thread_page_links_the_file_and_the_spouse_sees_no_trace_of_it(
        self, couple_case, send, client, sign_in
    ):
        _case, ada, ben = couple_case
        thread, attachments = send(ada, uploads=[a_file("letter.pdf")])
        link = reverse("messaging:attachment", args=[attachments[0].pk])

        sign_in(ada)
        hers = client.get(reverse("messaging:thread", kwargs={"pk": thread.pk}))
        assert link in hers.content.decode()
        assert "letter.pdf" in hers.content.decode()

        sign_in(ben)
        his = client.get(reverse("messaging:index"))
        assert "letter.pdf" not in his.content.decode()
        assert link not in his.content.decode()

    def test_an_administrator_reading_the_page_still_cannot_reply_with_a_file(
        self, couple_case, send, admin_user, client, sign_in
    ):
        """Reading is not a foothold for writing, attachment or otherwise."""
        _case, ada, _ben = couple_case
        thread, _ = send(ada, uploads=[])
        sign_in(admin_user)

        response = client.post(
            reverse("messaging:thread", kwargs={"pk": thread.pk}),
            {"body": "Adding a document to this.", "files": [a_file()]},
        )

        assert response.status_code == 403
        assert not MessageAttachment.objects.exists()
