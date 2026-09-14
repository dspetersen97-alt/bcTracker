"""
Which stored documents get a preview, and how the ones stored earlier catch up.

A counseling file is mostly PDFs — intake forms, consent, letters, a photographed
page of somebody's handwriting — and until PDFium was added only the photographs got
a thumbnail. So the documents page was a column of identical grey icons that had to be
opened one row at a time to find the form a counselor would have recognised on sight.

Two halves, and the second is the one that is easy to forget:

  * a PDF uploaded now gets a picture of its first page, encrypted under its own DEK
    like any other preview, and a Word file gets one of the page it was converted to;
  * the documents already in the store do not, because thumbnails are made on ingest.
    Every file uploaded before this change would keep its grey icon forever, which
    looks exactly like the feature not working. ``backfill_thumbnails`` is the answer,
    and it decrypts counselees' files in bulk — so what it prints, and that it can be
    run twice without doing anything twice, are part of the contract.
"""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.urls import reverse
from PIL import Image

from apps.audit.models import AuditEvent
from apps.counseling.models import Case, CaseMember
from apps.documents import services
from apps.documents.models import Document, Visibility
from tests.test_documents_ingest import real_pdf, word_document

pytestmark = pytest.mark.django_db


@pytest.fixture
def case(counselor, counselee):
    case = Case.objects.create(counselor=counselor, label="Ashford — individual")
    CaseMember.objects.create(case=case, counselee=counselee)
    return case


@pytest.fixture
def store(case, counselee):
    def _store(name="disclosure.pdf", data=None):
        return services.store_document(
            case=case,
            owner=counselee,
            upload=SimpleUploadedFile(name, real_pdf() if data is None else data),
            visibility=Visibility.PRIVATE,
        )

    return _store


def thumbnail_of(document):
    return Image.open(io.BytesIO(services.open_thumbnail(document)))


class TestWhatGetsAPreviewOnTheWayIn:
    def test_a_pdf_does(self, store):
        document = store()

        assert document.has_thumbnail
        assert thumbnail_of(document).format == "JPEG"

    def test_and_it_is_a_picture_of_the_page(self, store):
        """Not a generic icon dressed up as one: a letter page is taller than it is
        wide, and the thumbnail has to have kept that shape."""
        image = thumbnail_of(store())

        assert image.height > image.width
        assert max(image.size) == 320

    def test_a_word_file_gets_one_of_the_page_it_was_converted_to(self, store):
        """Honest rather than clever. The PDF is what a counselor will be reading, so
        it is what the preview should be of."""
        document = store(name="notes.docx", data=word_document("Session notes"))

        assert document.content_type == "application/pdf"
        assert document.has_thumbnail

    def test_a_text_file_does_not(self, store):
        """There is nothing to render, and the document page shows text in the viewer
        anyway. A missing thumbnail is the honest answer, not a failure."""
        document = store(name="notes.txt", data=b"what we talked about")

        assert not document.has_thumbnail

    def test_a_pdf_nothing_can_render_is_still_stored(self, store):
        """The order that matters: the document is kept, the preview is optional. A
        header and a trailer is a valid enough PDF to be identified and not one any
        renderer can draw."""
        document = store(data=b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n")

        assert document.pk
        assert not document.has_thumbnail

    def test_the_preview_is_encrypted_like_everything_else(self, store, _document_store):
        """A rendered page is a copy of the first page of somebody's disclosure. It
        gets the same treatment as the file it came from, and its own DEK."""
        document = store()

        assert document.thumbnail_wrapped_dek != document.wrapped_dek
        for path in _document_store.rglob("*"):
            if path.is_file():
                assert not path.read_bytes().startswith(b"\xff\xd8\xff"), (
                    f"{path.name} is a plaintext JPEG on disk"
                )

    def test_the_page_is_served_where_the_photographs_are(self, store, client, sign_in, counselee):
        """The route already existed for images and states image/jpeg; a rendered page
        arrives there as the same kind of bytes, produced by us, which is what makes
        that content type a fact rather than a claim."""
        document = store()
        sign_in(counselee)

        response = client.get(reverse("documents:thumbnail", args=[document.public_id]))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/jpeg"


@pytest.fixture
def stored_before_the_renderer(store, monkeypatch):
    """A PDF stored as it would have been before any of this existed.

    Faked by taking the renderer away for the upload, which is a truer fixture than
    editing the row afterwards: it produces exactly the document a real store holds —
    blob, keys, audit row, and no thumbnail.
    """

    def _store(**kwargs):
        monkeypatch.setattr(services.pdfpages, "render_first_page", lambda data: None)
        document = store(**kwargs)
        monkeypatch.undo()
        return document

    return _store


class TestCatchingUpTheDocumentsAlreadyStored:
    def test_a_pdf_that_missed_out_gets_its_preview(self, stored_before_the_renderer):
        document = stored_before_the_renderer()
        assert not document.has_thumbnail

        call_command("backfill_thumbnails")

        document.refresh_from_db()
        assert document.has_thumbnail
        assert thumbnail_of(document).format == "JPEG"

    def test_running_it_again_does_nothing(self, stored_before_the_renderer):
        """It is a one-off an operator runs after a deploy, so it will be run twice.
        The second run must not write a second blob, which would leak one per run."""
        document = stored_before_the_renderer()
        call_command("backfill_thumbnails")
        document.refresh_from_db()
        first_key = document.thumbnail_key

        call_command("backfill_thumbnails")

        document.refresh_from_db()
        assert document.thumbnail_key == first_key

    def test_a_dry_run_changes_nothing(self, stored_before_the_renderer):
        document = stored_before_the_renderer()

        call_command("backfill_thumbnails", "--dry-run")

        document.refresh_from_db()
        assert not document.has_thumbnail

    def test_it_can_be_worked_through_in_batches(self, stored_before_the_renderer):
        """Rendering is the slow part, and a store with years of documents in it
        should not have to be done in one run somebody is watching."""
        for index in range(3):
            stored_before_the_renderer(name=f"letter-{index}.pdf")

        call_command("backfill_thumbnails", "--limit", "2")

        assert Document.objects.filter(thumbnail_key__isnull=False).count() == 2

    def test_a_file_it_cannot_render_is_left_alone_rather_than_retried(
        self, stored_before_the_renderer
    ):
        """Reported as "no preview available" and not as an error. A PDF the renderer
        will not draw is a fact about that file, and it will still be true next run."""
        document = stored_before_the_renderer(data=b"%PDF-1.7\ntrailer\n")

        call_command("backfill_thumbnails")

        document.refresh_from_db()
        assert not document.has_thumbnail

    def test_a_withdrawn_document_is_not_given_a_new_preview(
        self, stored_before_the_renderer, counselor
    ):
        """The one exclusion worth stating. A soft-deleted document is withdrawn from
        the record, and rendering its first page would create a fresh plaintext
        derivative — encrypted, but new — of a file somebody asked to take down."""
        document = stored_before_the_renderer()
        services.soft_delete_document(document, actor=counselor)

        call_command("backfill_thumbnails")

        document.refresh_from_db()
        assert not document.has_thumbnail

    def test_nothing_it_does_is_a_disclosure(self, stored_before_the_renderer):
        """It decrypts files in bulk with nobody watching, so it writes no audit rows:
        the trail records who was shown what, and nobody has been shown anything. That
        it ran is in the deployment's own log, where an operational task belongs."""
        stored_before_the_renderer()
        before = AuditEvent.objects.count()

        call_command("backfill_thumbnails")

        assert AuditEvent.objects.count() == before
