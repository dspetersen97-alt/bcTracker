"""
Who can reach which document — the promise the product is sold on.

tests/test_access_matrix.py asks whether each role may reach each route with an
object it is legitimately connected to. This file asks what happens when it is
not, and the two questions it exists for are the two hardest promises in the
instruction document:

  * **counselees on a shared case see nothing of each other's.** A couple in
    marriage counseling each send disclosures they may not want the other to
    read, so the tempting query — "all documents on this case" — is the wrong one,
    and it is wrong in a way that looks correct in every screenshot.
  * **financial_admin never reaches a document at all.** Billing knows the case
    exists and who is on it. It does not learn that a document exists, which is
    why the refusals below are a mix of 403 and 404 rather than an empty list.

Everything goes through the real views, the real crypto, and the real store. A
test asserting on a hand-built queryset would pass while the view called
``.objects.all()``.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import Case, CaseMember
from apps.documents import ingest, scanning, services, storage
from apps.documents.crypto import DecryptionError
from apps.documents.models import Document, ScanStatus, Visibility
from tests.conftest import jpeg_bytes

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\nthe contents of a counselee's disclosure\ntrailer\n"


@pytest.fixture
def couple_case(counselor, make_user):
    """A shared case: one counselor, two counselees who share a surname.

    The couple's case is the shape that makes isolation non-trivial, so it is the
    default here rather than a special case. Given names differ so an assertion
    about what a page shows can tell the two apart.
    """
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage", kind="couple")
    ada = make_user(Role.COUNSELEE, first_name="Ada", last_name="Ashford")
    ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
    CaseMember.objects.create(case=case, counselee=ada)
    CaseMember.objects.create(case=case, counselee=ben)
    return case, ada, ben


@pytest.fixture
def store():
    """Store a document the way the view does, through the real service."""

    def _store(
        case, owner, *, name="disclosure.pdf", data=PDF, visibility=Visibility.PRIVATE, **kw
    ):
        return services.store_document(
            case=case,
            owner=owner,
            upload=SimpleUploadedFile(name, data),
            visibility=visibility,
            **kw,
        )

    return _store


def upload_payload(name="homework.pdf", data=PDF, **extra):
    return {
        "file": SimpleUploadedFile(name, data),
        "title": "",
        "description": "",
        "kind": "homework",
        **extra,
    }


class TestDocumentVisibility:
    """The queryset layer, asked directly. Every view inherits what this says."""

    def test_a_counselee_sees_their_own_upload(self, couple_case, store):
        case, ada, _ben = couple_case
        mine = store(case, ada)

        assert list(Document.objects.for_actor(ada)) == [mine]

    def test_a_counselee_does_not_see_the_others_private_upload(self, couple_case, store):
        """The promise. Ada and Ben are on one case and share nothing.

        Note what is *not* asserted: there is no "1 hidden document" anywhere. A
        count would tell Ada that Ben had sent something, which is the disclosure
        itself in a smaller font.
        """
        case, ada, ben = couple_case
        store(case, ben, name="bens-private-letter.pdf")

        assert not Document.objects.for_actor(ada).exists()

    def test_sharing_with_the_case_is_what_makes_it_visible(self, couple_case, store, counselor):
        case, ada, ben = couple_case
        document = store(case, ben)

        services.share_with_case(document, actor=counselor)

        assert list(Document.objects.for_actor(ada)) == [document]

    def test_a_counselor_sees_everything_on_their_own_case(self, couple_case, store, counselor):
        """What "private" does and does not mean.

        Private is private from the other counselee, not from the counselor: the
        counselor is a party to everything on their case by definition, and a
        two-way document exchange with a counselor who cannot read one direction
        would be pointless.
        """
        case, ada, ben = couple_case
        store(case, ada)
        store(case, ben)

        assert Document.objects.for_actor(counselor).count() == 2

    def test_a_counselor_sees_nothing_of_another_counselors_case(
        self, couple_case, store, other_counselor
    ):
        case, ada, _ben = couple_case
        store(case, ada)

        assert not Document.objects.for_actor(other_counselor).exists()

    def test_an_admin_sees_everything(self, couple_case, store, admin_user):
        case, ada, _ben = couple_case
        store(case, ada)

        assert Document.objects.for_actor(admin_user).count() == 1

    def test_financial_admin_sees_nothing_ever(self, couple_case, store, financial_admin):
        """The single most important line in apps/documents/models.py, asserted.

        financial_admin can see the case — billing needs the caseload — so this is
        not a side effect of case scoping. It is an explicit none().
        """
        case, ada, ben = couple_case
        store(case, ada)
        store(case, ben, visibility=Visibility.CASE_SHARED)

        assert Case.objects.for_actor(financial_admin).count() == 1, "the case itself is visible"
        assert not Document.objects.for_actor(financial_admin).exists()

    def test_a_withdrawn_document_is_invisible_even_to_an_admin(
        self, couple_case, store, admin_user, counselor
    ):
        """Soft delete hides the row from every actor, including an administrator.

        ``all_objects`` exists so a retention job — or an administrator who knows
        to reach for it — can still find it. The default manager is what every
        view uses, and it must not be the place someone accidentally gets it back.
        """
        case, ada, _ben = couple_case
        document = store(case, ada)

        services.soft_delete_document(document, actor=ada)

        assert not Document.objects.for_actor(admin_user).exists()
        assert not Document.objects.for_actor(counselor).exists()
        assert Document.all_objects.filter(pk=document.pk).exists()

    def test_withdrawing_the_case_hides_the_documents_on_it(self, couple_case, store, counselor):
        """Inherited from CaseScopedQuerySet, and worth proving from this side.

        A soft-deleted case must not leave its documents reachable — that is the
        kind of gap that only shows up in the app that was added last.
        """
        case, ada, _ben = couple_case
        store(case, ada)

        case.soft_delete()

        assert not Document.objects.for_actor(counselor).exists()
        assert not Document.objects.for_actor(ada).exists()

    def test_ending_a_membership_ends_access_to_their_own_uploads(self, couple_case, store):
        """Deliberate, and the direction may look surprising.

        The documents belong to the counseling relationship. Someone removed from
        a case stops seeing it, including what they sent themselves; the counselor
        keeps the history, because that is what the record is for.
        """
        case, ada, _ben = couple_case
        store(case, ada)

        CaseMember.objects.get(case=case, counselee=ada).end()

        assert not Document.objects.for_actor(ada).exists()


class TestSharedCaseIsolationThroughTheViews:
    """The same promise, asserted where a mistake would actually happen."""

    @pytest.fixture
    def bens_letter(self, couple_case, store):
        _case, _ada, ben = couple_case
        return store(couple_case[0], ben, name="bens-letter.pdf")

    @pytest.mark.parametrize(
        ("route", "method"),
        [
            ("documents:detail", "get"),
            ("documents:download", "get"),
            ("documents:thumbnail", "get"),
            ("documents:edit", "get"),
            ("documents:share", "post"),
            ("documents:delete", "post"),
        ],
    )
    def test_the_other_counselee_gets_404_on_every_route(
        self, couple_case, bens_letter, client, sign_in, route, method
    ):
        """404, not 403.

        A 403 would confirm the document exists, and "your spouse has uploaded
        something you may not read" is a disclosure on its own.
        """
        _case, ada, _ben = couple_case
        sign_in(ada)

        response = getattr(client, method)(reverse(route, kwargs={"pk": bens_letter.pk}))

        assert response.status_code == 404

    def test_the_case_document_list_does_not_name_the_others_file(
        self, couple_case, bens_letter, client, sign_in
    ):
        case, ada, _ben = couple_case
        sign_in(ada)

        response = client.get(reverse("documents:case_documents", kwargs={"case_pk": case.pk}))

        assert response.status_code == 200
        assert b"bens-letter" not in response.content
        assert list(response.context["documents"]) == []

    def test_the_other_counselee_is_not_named_either(
        self, couple_case, bens_letter, client, sign_in, counselor
    ):
        """Even once the file is shared, whose it is stays between them and the counselor.

        The template shows an owner column, and on a shared case that column is
        exactly the kind of incidental disclosure this project is trying to avoid.
        """
        case, ada, ben = couple_case
        services.share_with_case(bens_letter, actor=counselor)
        sign_in(ada)

        response = client.get(reverse("documents:case_documents", kwargs={"case_pk": case.pk}))

        assert ben.first_name.encode() not in response.content
        assert b"Shared with the case" in response.content

    def test_the_counselor_still_sees_whose_document_it_is(
        self, couple_case, bens_letter, client, sign_in, counselor
    ):
        """The other half of the same rule: for the counselor, provenance is the record."""
        case, _ada, ben = couple_case
        sign_in(counselor)

        response = client.get(reverse("documents:case_documents", kwargs={"case_pk": case.pk}))

        assert ben.first_name.encode() in response.content

    def test_a_shared_document_can_be_read_but_not_changed(
        self, couple_case, bens_letter, client, sign_in, counselor
    ):
        case, ada, _ben = couple_case
        services.share_with_case(bens_letter, actor=counselor)
        sign_in(ada)

        assert client.get(reverse("documents:detail", args=[bens_letter.pk])).status_code == 200
        assert client.get(reverse("documents:download", args=[bens_letter.pk])).status_code == 200
        # Reading is not owning: Ada can neither relabel it, withdraw it, nor take
        # back the counselor's decision to share it.
        assert client.get(reverse("documents:edit", args=[bens_letter.pk])).status_code == 403
        assert client.post(reverse("documents:delete", args=[bens_letter.pk])).status_code == 403
        assert client.post(reverse("documents:share", args=[bens_letter.pk])).status_code == 403

    def test_a_counselor_from_another_case_gets_404(
        self, bens_letter, client, sign_in, other_counselor
    ):
        sign_in(other_counselor)

        assert client.get(reverse("documents:detail", args=[bens_letter.pk])).status_code == 404
        assert client.get(reverse("documents:download", args=[bens_letter.pk])).status_code == 404


class TestWhatBillingCanReach:
    """financial_admin, route by route. The answer is always no."""

    def test_the_case_document_list_is_refused_rather_than_shown_empty(
        self, couple_case, store, client, sign_in, financial_admin
    ):
        """403 rather than a page reading "nothing here".

        An empty list is a claim about content. The point is that billing does not
        get to ask the question at all.
        """
        case, ada, _ben = couple_case
        store(case, ada)
        sign_in(financial_admin)

        response = client.get(reverse("documents:case_documents", kwargs={"case_pk": case.pk}))

        assert response.status_code == 403

    def test_the_cross_case_index_is_refused_too(self, client, sign_in, financial_admin):
        sign_in(financial_admin)

        assert client.get(reverse("documents:my_documents")).status_code == 403

    def test_uploading_on_a_case_is_refused(self, couple_case, client, sign_in, financial_admin):
        case, _ada, _ben = couple_case
        sign_in(financial_admin)

        assert (
            client.get(reverse("documents:upload", kwargs={"case_pk": case.pk})).status_code == 403
        )

    def test_a_document_does_not_exist_as_far_as_billing_is_concerned(
        self, couple_case, store, client, sign_in, financial_admin
    ):
        case, ada, _ben = couple_case
        document = store(case, ada)
        sign_in(financial_admin)

        assert client.get(reverse("documents:detail", args=[document.pk])).status_code == 404
        assert client.get(reverse("documents:download", args=[document.pk])).status_code == 404

    def test_the_permission_itself_is_never_granted(self, couple_case, store, financial_admin):
        """Belt and braces: the predicate says no even holding the object.

        The queryset already hides the row, so this is the second of the two
        independent layers — a future view that forgot ``for_actor`` would still
        be refused here.
        """
        case, ada, _ben = couple_case
        document = store(case, ada, visibility=Visibility.CASE_SHARED)

        assert not financial_admin.has_perm("documents.view_document", document)
        assert not financial_admin.has_perm("documents.add_document", case)
        assert not financial_admin.has_perm("documents.share_document", document)


class TestUploading:
    def test_a_counselee_can_send_their_counselor_a_document(
        self, couple_case, client, sign_in, counselor
    ):
        """Half the product: the two-way exchange the instruction document asks for."""
        case, ada, _ben = couple_case
        sign_in(ada)

        response = client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}),
            upload_payload(name="week-one.pdf"),
        )

        document = Document.objects.get()
        assert response.status_code == 302
        assert document.owner == ada
        assert document.case == case
        assert document.original_filename == "week-one.pdf"
        assert document.content_type == "application/pdf"
        assert document.byte_size == len(PDF)
        assert document in Document.objects.for_actor(counselor)

    def test_what_a_counselee_sends_is_private_by_default(self, couple_case, client, sign_in):
        """The default carries the privacy promise, so it is asserted on its own.

        Defaulting the other way would be a disclosure the ministry could not take
        back — every upload would reach the spouse before anyone noticed.
        """
        case, ada, _ben = couple_case
        sign_in(ada)

        client.post(reverse("documents:upload", kwargs={"case_pk": case.pk}), upload_payload())

        assert Document.objects.get().visibility == Visibility.PRIVATE

    def test_a_counselee_cannot_choose_to_share_by_posting_the_field(
        self, couple_case, client, sign_in
    ):
        """The form deletes the field; this proves deleting beats disabling.

        A hand-built request submits whatever it likes, so the check that matters
        is what the *view* does with it — and it forces PRIVATE regardless.
        """
        case, ada, _ben = couple_case
        sign_in(ada)

        client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}),
            upload_payload(visibility=Visibility.CASE_SHARED),
        )

        assert Document.objects.get().visibility == Visibility.PRIVATE

    def test_a_counselor_may_publish_a_handout_to_the_whole_case(
        self, couple_case, client, sign_in, counselor
    ):
        case, ada, ben = couple_case
        sign_in(counselor)

        client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}),
            upload_payload(name="handout.pdf", visibility=Visibility.CASE_SHARED),
        )

        document = Document.objects.get()
        assert document.visibility == Visibility.CASE_SHARED
        assert document in Document.objects.for_actor(ada)
        assert document in Document.objects.for_actor(ben)

    def test_uploading_onto_another_counselors_case_is_a_404(
        self, couple_case, client, sign_in, other_counselor
    ):
        case, _ada, _ben = couple_case
        sign_in(other_counselor)

        response = client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}), upload_payload()
        )

        assert response.status_code == 404
        assert not Document.objects.exists()

    def test_a_former_counselee_cannot_upload_anything_more(self, couple_case, client, sign_in):
        case, ada, _ben = couple_case
        CaseMember.objects.get(case=case, counselee=ada).end()
        sign_in(ada)

        response = client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}), upload_payload()
        )

        assert response.status_code == 404
        assert not Document.objects.exists()

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            # Refused by the extension allowlist, before anything is read.
            ("notes.zip", b"PK\x03\x04nope"),
            ("setup.exe", b"MZ\x90\x00"),
            # Refused by the signature check: the extension is allowed and the
            # bytes disagree with it.
            ("invoice.pdf", b"MZ\x90\x00\x03\x00\x00\x00"),
        ],
    )
    def test_a_file_we_do_not_accept_is_refused_and_nothing_is_stored(
        self, couple_case, client, sign_in, _document_store, name, data
    ):
        case, ada, _ben = couple_case
        sign_in(ada)

        response = client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}),
            upload_payload(name=name, data=data),
        )

        assert response.status_code == 200, "the form should re-render with an error"
        assert response.context["form"].errors
        assert not Document.objects.exists()
        assert not list(_document_store.rglob("*")), "no blob, not even a staging file"

    def test_a_file_over_the_limit_is_refused(self, couple_case, client, sign_in, settings):
        case, ada, _ben = couple_case
        settings.DOCUMENT_MAX_BYTES = 100
        sign_in(ada)

        response = client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}),
            upload_payload(data=b"%PDF-1.7\n" + b"x" * 500),
        )

        assert response.context["form"].errors
        assert not Document.objects.exists()

    def test_an_infected_file_never_reaches_the_disk(
        self, couple_case, client, sign_in, monkeypatch, _document_store
    ):
        """Scanning happens before storage, so there is no "pending" window.

        Storing first and scanning after would mean malware sitting on the volume
        while something is trusted not to serve it.
        """

        def infected(upload):
            raise scanning.InfectedFile("Win.Test.EICAR_HDB-1")

        monkeypatch.setattr(ingest.scanning, "scan", infected)
        case, ada, _ben = couple_case
        sign_in(ada)

        response = client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}), upload_payload()
        )

        assert response.context["form"].errors
        assert not Document.objects.exists()
        assert not list(_document_store.rglob("*"))
        rejection = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_SCAN_REJECTED)
        assert rejection.metadata["signature"] == "Win.Test.EICAR_HDB-1"
        assert rejection.actor == ada

    def test_an_unreachable_scanner_fails_the_upload_rather_than_storing_it(
        self, couple_case, client, sign_in, monkeypatch, _document_store
    ):
        """Fail-closed, and deliberately not dressed up as a bad file.

        A scanner that is down is an operational failure. Reporting it as "your
        file was rejected" would send a counselee away to try a different file
        while nobody investigates the actual problem.
        """

        def unavailable(upload):
            raise scanning.ScannerUnavailable("connection refused")

        monkeypatch.setattr(ingest.scanning, "scan", unavailable)
        case, ada, _ben = couple_case
        sign_in(ada)

        with pytest.raises(scanning.ScannerUnavailable):
            client.post(reverse("documents:upload", kwargs={"case_pk": case.pk}), upload_payload())

        assert not Document.objects.exists()
        assert not list(_document_store.rglob("*"))

    def test_the_stored_bytes_are_not_the_uploaded_bytes(
        self, couple_case, client, sign_in, _document_store
    ):
        """The whole point of the volume, asserted at the volume.

        Every other test in this file would pass if store_document wrote the file
        straight through.
        """
        case, ada, _ben = couple_case
        sign_in(ada)

        client.post(reverse("documents:upload", kwargs={"case_pk": case.pk}), upload_payload())

        blobs = [path for path in _document_store.rglob("*") if path.is_file()]
        assert blobs, "something should have been written"
        for path in blobs:
            assert b"disclosure" not in path.read_bytes()

    def test_an_upload_is_recorded(self, couple_case, client, sign_in):
        case, ada, _ben = couple_case
        sign_in(ada)

        client.post(reverse("documents:upload", kwargs={"case_pk": case.pk}), upload_payload())

        document = Document.objects.get()
        event = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_UPLOADED)
        assert event.actor == ada
        assert event.target_type == "documents.Document"
        assert event.target_id == str(document.pk)
        assert event.case_id_snapshot == str(case.pk)
        assert event.metadata["visibility"] == Visibility.PRIVATE


class TestPhotographs:
    def test_a_photos_location_is_gone_from_what_gets_stored(self, couple_case, client, sign_in):
        """End to end, because the strip is only worth anything if it is wired up.

        tests/test_documents_ingest.py proves strip_metadata works. This proves the
        upload path actually calls it — and that what comes back out of the store
        is the stripped version, not the original.
        """
        from PIL import Image
        from PIL.TiffImagePlugin import IFDRational

        exif = Image.Exif()
        gps = exif.get_ifd(0x8825)
        gps[1] = "N"
        gps[2] = (IFDRational(40), IFDRational(44), IFDRational(54, 100))
        original = jpeg_bytes(size=(32, 24), exif=exif)
        case, ada, _ben = couple_case
        sign_in(ada)

        client.post(
            reverse("documents:upload", kwargs={"case_pk": case.pk}),
            upload_payload(name="kitchen-table.jpg", data=original),
        )

        document = Document.objects.get()
        downloaded = b"".join(
            client.get(reverse("documents:download", args=[document.pk])).streaming_content
        )
        assert not Image.open(io.BytesIO(downloaded)).getexif().get_ifd(0x8825)

    def test_a_thumbnail_is_encrypted_like_anything_else(self, couple_case, store, _document_store):
        """No plaintext derivative on disk, for exactly the files showing a face."""
        case, ada, _ben = couple_case

        document = store(case, ada, name="photo.jpg", data=jpeg_bytes(size=(600, 400)))

        assert document.has_thumbnail
        assert document.thumbnail_key != document.storage_key
        for path in _document_store.rglob("*"):
            if path.is_file():
                assert not path.read_bytes().startswith(b"\xff\xd8\xff"), (
                    f"{path.name} is a plaintext JPEG on disk"
                )

    def test_the_thumbnail_has_its_own_key(self, couple_case, store):
        """So a preview and its source share no keystream, and no key material."""
        case, ada, _ben = couple_case

        document = store(case, ada, name="photo.jpg", data=jpeg_bytes())

        assert document.thumbnail_wrapped_dek != document.wrapped_dek

    def test_a_preview_is_served_inline_but_sandboxed(self, couple_case, store, client, sign_in):
        """Inline is safe here and only here: we re-encoded these bytes ourselves.

        The content type is a fact about what Pillow wrote rather than a claim
        carried over from the upload, and the sandbox header means even a mistake
        about that cannot run as our origin.
        """
        case, ada, _ben = couple_case
        document = store(case, ada, name="photo.jpg", data=jpeg_bytes())
        sign_in(ada)

        response = client.get(reverse("documents:thumbnail", args=[document.pk]))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/jpeg"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in response["Content-Security-Policy"]

    def test_a_document_with_no_thumbnail_has_no_preview_route(
        self, couple_case, store, client, sign_in
    ):
        case, ada, _ben = couple_case
        document = store(case, ada)
        sign_in(ada)

        assert client.get(reverse("documents:thumbnail", args=[document.pk])).status_code == 404


class TestDownloading:
    def test_the_bytes_come_back_exactly(self, couple_case, store, client, sign_in):
        case, ada, _ben = couple_case
        document = store(case, ada)
        sign_in(ada)

        response = client.get(reverse("documents:download", args=[document.pk]))

        assert b"".join(response.streaming_content) == PDF

    def test_a_download_is_always_an_attachment(self, couple_case, store, client, sign_in):
        """Never inline, even for something a browser could render.

        An inline file served from our own origin runs as our origin, and the
        response is committed before a frame fails authentication — so a truncated
        attachment is a broken file someone retries rather than a partial page.
        """
        case, ada, _ben = couple_case
        document = store(case, ada)
        sign_in(ada)

        response = client.get(reverse("documents:download", args=[document.pk]))

        assert response["Content-Disposition"].startswith("attachment;")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "no-store" in response["Cache-Control"]
        assert response["Referrer-Policy"] == "no-referrer"

    def test_the_content_type_is_the_one_we_sniffed(self, couple_case, client, sign_in):
        """Not the one the browser declared, which is an instruction, not a fact."""
        case, ada, _ben = couple_case
        document = services.store_document(
            case=case,
            owner=ada,
            upload=SimpleUploadedFile("disclosure.pdf", PDF, content_type="text/html"),
        )
        sign_in(ada)

        response = client.get(reverse("documents:download", args=[document.pk]))

        assert response["Content-Type"] == "application/pdf"

    def test_an_awkward_filename_cannot_break_the_header(self, couple_case, store, client, sign_in):
        """The filename came from a browser, so it is untrusted input in a header."""
        case, ada, _ben = couple_case
        document = store(case, ada, name='we"ird; drop.pdf')
        sign_in(ada)

        disposition = client.get(reverse("documents:download", args=[document.pk]))[
            "Content-Disposition"
        ]

        assert '"weird drop.pdf"' in disposition
        assert "\n" not in disposition and "\r" not in disposition

    def test_a_non_ascii_filename_survives(self, couple_case, store, client, sign_in):
        case, ada, _ben = couple_case
        document = store(case, ada, name="bénédiction.pdf")
        sign_in(ada)

        disposition = client.get(reverse("documents:download", args=[document.pk]))[
            "Content-Disposition"
        ]

        assert "filename*=UTF-8''b%C3%A9n%C3%A9diction.pdf" in disposition

    def test_a_download_is_recorded_before_a_byte_is_decrypted(
        self, couple_case, store, client, sign_in
    ):
        case, ada, _ben = couple_case
        document = store(case, ada)
        sign_in(ada)

        client.get(reverse("documents:download", args=[document.pk]))

        event = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_DOWNLOADED)
        assert event.actor == ada
        assert event.target_id == str(document.pk)
        assert event.metadata["filename"] == "disclosure.pdf"

    def test_an_unrecordable_download_serves_nothing(
        self, couple_case, store, client, sign_in, monkeypatch
    ):
        """The one place the audit trail is allowed to break the user's request.

        An unlogged disclosure is exactly what the trail exists to prevent, so if
        the row cannot be written the file is not served. Everywhere else,
        ``record`` swallows the failure instead.
        """

        def broken(*args, **kwargs):
            raise RuntimeError("the audit table is unavailable")

        monkeypatch.setattr(services, "record_or_raise", broken)
        case, ada, _ben = couple_case
        document = store(case, ada)
        sign_in(ada)

        with pytest.raises(RuntimeError):
            client.get(reverse("documents:download", args=[document.pk]))

    def test_a_missing_blob_is_a_404_rather_than_a_crash(self, couple_case, store, client, sign_in):
        """What a restore that missed the document volume looks like."""
        case, ada, _ben = couple_case
        document = store(case, ada)
        storage.delete_blob(document.storage_key)
        sign_in(ada)

        assert client.get(reverse("documents:download", args=[document.pk])).status_code == 404

    def test_a_tampered_blob_does_not_produce_a_plausible_file(
        self, couple_case, store, client, sign_in
    ):
        """Streaming's honest limitation, stated as a test.

        The status line is already sent when a frame fails authentication, so the
        response cannot become a 404 — the stream raises instead. What must never
        happen is quietly returning the bytes that did authenticate as though they
        were the whole document.
        """
        case, ada, _ben = couple_case
        document = store(case, ada)
        path = storage.blob_path(document.storage_key)
        corrupted = bytearray(path.read_bytes())
        corrupted[-1] ^= 0x01
        path.write_bytes(bytes(corrupted))
        sign_in(ada)

        response = client.get(reverse("documents:download", args=[document.pk]))

        with pytest.raises(DecryptionError):
            b"".join(response.streaming_content)

    def test_integrity_can_be_verified_without_serving_the_file(self, couple_case, store):
        """For the restore drill: the database restoring is the easy half."""
        case, ada, _ben = couple_case
        document = store(case, ada)

        assert services.verify_integrity(document) is True

    def test_a_corrupted_document_fails_verification(self, couple_case, store):
        case, ada, _ben = couple_case
        document = store(case, ada)
        path = storage.blob_path(document.storage_key)
        corrupted = bytearray(path.read_bytes())
        corrupted[-1] ^= 0x01
        path.write_bytes(bytes(corrupted))

        assert services.verify_integrity(document) is False


class TestSharing:
    def test_the_counselor_decides_and_it_is_recorded(
        self, couple_case, store, client, sign_in, counselor
    ):
        case, ada, ben = couple_case
        document = store(case, ben)
        sign_in(counselor)

        response = client.post(reverse("documents:share", args=[document.pk]))

        document.refresh_from_db()
        assert response.status_code == 302
        assert document.visibility == Visibility.CASE_SHARED
        assert document in Document.objects.for_actor(ada)
        assert AuditEvent.objects.filter(
            verb=AuditVerb.DOCUMENT_SHARED, target_id=str(document.pk)
        ).exists()

    def test_the_uploader_cannot_publish_their_own_file_to_the_case(
        self, couple_case, store, client, sign_in
    ):
        """Ben owns it and is still refused.

        On a family case the uploader may not know who else is on it, so making
        this the counselor's decision is what stops "nothing of the other's" from
        being something one counselee can waive on the other's behalf.
        """
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)

        response = client.post(reverse("documents:share", args=[document.pk]))

        document.refresh_from_db()
        assert response.status_code == 403
        assert document.visibility == Visibility.PRIVATE

    def test_a_refusal_is_recorded_too(self, couple_case, store, client, sign_in):
        """Someone trying to reach past their own case is worth a row."""
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)

        client.post(reverse("documents:share", args=[document.pk]))

        denial = AuditEvent.objects.get(verb=AuditVerb.ACCESS_DENIED)
        assert denial.actor == ben
        assert denial.metadata["permission"] == "documents.share_document"

    def test_unsharing_takes_the_visibility_back(
        self, couple_case, store, client, sign_in, counselor
    ):
        case, ada, ben = couple_case
        document = store(case, ben, visibility=Visibility.CASE_SHARED)
        sign_in(counselor)

        client.post(reverse("documents:share", args=[document.pk]))

        document.refresh_from_db()
        assert document.visibility == Visibility.PRIVATE
        assert document not in Document.objects.for_actor(ada)
        assert AuditEvent.objects.filter(verb=AuditVerb.DOCUMENT_UNSHARED).exists()

    def test_the_warning_says_what_sharing_cannot_undo(
        self, couple_case, store, client, sign_in, counselor
    ):
        """Sharing is not reversible in the way a setting suggests.

        Anyone who read it keeps what they read, and the counselor is told so at
        the moment they decide rather than in a help page.
        """
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(counselor)

        response = client.post(reverse("documents:share", args=[document.pk]), follow=True)

        assert any("keeps what they have read" in str(m) for m in response.context["messages"])


class TestWithdrawing:
    def test_the_uploader_may_withdraw_their_own_document(
        self, couple_case, store, client, sign_in, counselor
    ):
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)

        response = client.post(reverse("documents:delete", args=[document.pk]))

        assert response.status_code == 302
        assert not Document.objects.for_actor(ben).exists()
        assert not Document.objects.for_actor(counselor).exists()

    def test_the_ciphertext_survives_a_withdrawal(self, couple_case, store, client, sign_in):
        """A withdrawn document is still part of the counseling record.

        Retention policy decides when it is really gone — not a click, and not a
        counselee having second thoughts after a disclosure.
        """
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)

        client.post(reverse("documents:delete", args=[document.pk]))

        assert storage.blob_exists(document.storage_key)
        assert Document.all_objects.get(pk=document.pk).deleted_at is not None

    def test_withdrawing_is_recorded_with_the_filename(self, couple_case, store, client, sign_in):
        """The counselor's record that it existed, which is what the message promises."""
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)

        client.post(reverse("documents:delete", args=[document.pk]))

        event = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_DELETED)
        assert event.actor == ben
        assert event.metadata["filename"] == "disclosure.pdf"

    def test_a_counselee_cannot_withdraw_the_counselors_handout(
        self, couple_case, store, client, sign_in, counselor
    ):
        case, ada, _ben = couple_case
        handout = store(case, counselor, name="handout.pdf", visibility=Visibility.CASE_SHARED)
        sign_in(ada)

        response = client.post(reverse("documents:delete", args=[handout.pk]))

        assert response.status_code == 403
        assert Document.objects.filter(pk=handout.pk).exists()

    def test_a_withdrawn_document_cannot_be_downloaded_again(
        self, couple_case, store, client, sign_in
    ):
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)
        client.post(reverse("documents:delete", args=[document.pk]))

        assert client.get(reverse("documents:download", args=[document.pk])).status_code == 404


class TestEditingTheLabels:
    def test_the_uploader_may_relabel_their_own_document(self, couple_case, store, client, sign_in):
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)

        client.post(
            reverse("documents:edit", args=[document.pk]),
            {"title": "Week one homework", "description": "Finished late.", "kind": "homework"},
        )

        document.refresh_from_db()
        assert document.title == "Week one homework"
        assert AuditEvent.objects.filter(verb=AuditVerb.DOCUMENT_UPDATED).exists()

    def test_editing_cannot_change_who_can_see_it(self, couple_case, store, client, sign_in):
        """Visibility is not on this form, and posting it changes nothing.

        Folding a disclosure into a general edit would make it look like a typo fix
        in the audit trail, which is where someone would later go looking.
        """
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(ben)

        client.post(
            reverse("documents:edit", args=[document.pk]),
            {
                "title": "Homework",
                "description": "",
                "kind": "homework",
                "visibility": Visibility.CASE_SHARED,
            },
        )

        document.refresh_from_db()
        assert document.visibility == Visibility.PRIVATE

    def test_the_bytes_are_not_editable_at_all(self, couple_case, store, client, sign_in):
        """A correction is a new upload.

        The recorded hash describes what was uploaded, and the audit rows point at
        it. Replacing the payload under the same row would make both describe
        something that no longer exists.
        """
        from apps.documents.forms import DocumentEditForm

        assert "file" not in DocumentEditForm().fields
        assert set(DocumentEditForm().fields) == {"title", "description", "kind"}


class TestTheRecordOfWhoLooked:
    def test_opening_the_page_is_recorded(self, couple_case, store, client, sign_in, counselor):
        """Viewing the metadata is an access too.

        A counselor reading the title and the description of a disclosure has read
        something, even if they never clicked download.
        """
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(counselor)

        client.get(reverse("documents:detail", args=[document.pk]))

        event = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_VIEWED)
        assert event.actor == counselor
        assert event.actor_role == Role.COUNSELOR
        assert event.case_id_snapshot == str(case.pk)

    def test_the_trail_survives_the_document_being_withdrawn(
        self, couple_case, store, client, sign_in, counselor
    ):
        """Target is stored as (type, id) strings, not a foreign key.

        So the record of who read a document outlives the document, which is the
        whole reason the audit table does not use a GenericForeignKey.
        """
        case, _ada, ben = couple_case
        document = store(case, ben)
        sign_in(counselor)
        client.get(reverse("documents:download", args=[document.pk]))

        services.soft_delete_document(document, actor=counselor)

        assert AuditEvent.objects.filter(
            verb=AuditVerb.DOCUMENT_DOWNLOADED, target_id=str(document.pk)
        ).exists()


class TestTheStoredRow:
    def test_the_hash_describes_what_a_download_produces(self, couple_case, store):
        """Recorded after any EXIF strip, so it identifies the served bytes.

        Hashing the upload instead would make every integrity check on a photo
        fail, and the drill would learn to ignore it.
        """
        import hashlib

        case, ada, _ben = couple_case
        document = store(case, ada, name="photo.jpg", data=jpeg_bytes(size=(40, 30)))

        served = b"".join(services.open_document(document, actor=ada))
        assert document.sha256 == hashlib.sha256(served).hexdigest()
        assert document.byte_size == len(served)

    def test_the_scan_verdict_is_kept(self, couple_case, store):
        """SKIPPED in tests, and visible on the page, so a production skip is noticed."""
        case, ada, _ben = couple_case

        document = store(case, ada)

        assert document.scan_status == ScanStatus.SKIPPED

    def test_an_infected_row_cannot_exist_even_by_mistake(self, couple_case, store):
        """A database constraint, because a bug upstream must not end in malware.

        ``store_document`` refuses an infected upload; this is the second line, so
        a future code path that forgets cannot leave one on the volume.
        """
        from django.db import IntegrityError, transaction

        case, ada, _ben = couple_case
        document = store(case, ada)

        document.scan_status = ScanStatus.INFECTED
        with pytest.raises(IntegrityError), transaction.atomic():
            document.save(update_fields=["scan_status"])
