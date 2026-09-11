"""
Showing a document in the browser instead of handing it over as a file.

A counselor asked for this because the alternative is a shared church computer
whose Downloads folder fills up with counselees' disclosures — a copy of the
thing this application exists to keep encrypted, sitting in plaintext outside it,
where nobody thinks to delete it. So "view" is a real feature, and it is the one
place in the application that tells a browser to *render* what somebody uploaded.

Which makes the allowlist load-bearing, and these tests are mostly about it:

  * only the types in ``apps.core.downloads.INLINE_TYPES`` are ever inline, and
    the bytes have to agree with the type before they are served;
  * an SVG can neither be uploaded nor served inline — two independent refusals,
    because inline SVG from our own origin is cross-site scripting with a
    counselee's session attached;
  * a preview is exactly as much of a disclosure as a download, so it goes through
    the same permission check and lands in the audit trail as one.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditEvent, AuditVerb
from apps.core.downloads import may_be_shown_inline
from apps.counseling.models import Case, CaseMember
from apps.documents import services
from apps.documents.models import Visibility
from tests.conftest import jpeg_bytes

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\nthe contents of a counselee's disclosure\ntrailer\n"


@pytest.fixture
def case(counselor, counselee):
    case = Case.objects.create(counselor=counselor, label="Ashford — individual")
    CaseMember.objects.create(case=case, counselee=counselee)
    return case


@pytest.fixture
def store(case, counselee):
    def _store(name="disclosure.pdf", data=PDF, owner=None, **kw):
        return services.store_document(
            case=case,
            owner=owner or counselee,
            upload=SimpleUploadedFile(name, data),
            visibility=Visibility.PRIVATE,
            **kw,
        )

    return _store


def preview_url(document):
    return reverse("documents:preview", args=[document.pk])


class TestWhatMayBeShownInline:
    """The allowlist, asked directly. Every response below inherits this answer."""

    @pytest.mark.parametrize(
        "content_type,head",
        [
            ("application/pdf", b"%PDF-1.7\n"),
            ("image/jpeg", b"\xff\xd8\xff\xe0 the rest of a jpeg"),
            ("image/png", b"\x89PNG\r\n\x1a\n and the rest"),
            ("text/plain", "a counselee's notes — with an em dash".encode()),
        ],
    )
    def test_the_types_a_browser_can_be_trusted_with(self, content_type, head):
        assert may_be_shown_inline(content_type, head) is True

    @pytest.mark.parametrize(
        "content_type",
        [
            # The one that matters: an SVG is a document with scripts in it.
            "image/svg+xml",
            "text/html",
            "application/xml",
            "image/heic",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/octet-stream",
            "",
        ],
    )
    def test_everything_else_is_refused_whatever_the_bytes_say(self, content_type):
        assert may_be_shown_inline(content_type, b"<svg onload=alert(1)>") is False

    def test_a_type_whose_bytes_disagree_with_it_is_refused(self):
        """Belt and braces. The stored type was sniffed at upload and is sound, so
        this can only fail on a row that was written by something other than the
        ingest pipeline — which is exactly when a second check is worth having."""
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'>"
        assert may_be_shown_inline("image/png", svg) is False
        assert may_be_shown_inline("application/pdf", b"\x89PNG\r\n\x1a\n") is False

    def test_binary_claiming_to_be_text_is_refused(self):
        assert may_be_shown_inline("text/plain", b"MZ\x00\x00\x90") is False


class TestTheResponse:
    def test_a_pdf_comes_back_inline_with_its_bytes(self, client, sign_in, counselee, store):
        document = store()
        sign_in(counselee)

        response = client.get(preview_url(document))

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].startswith("inline;")
        assert b"".join(response.streaming_content) == PDF

    def test_the_hardening_a_download_gets_is_all_still_there(
        self, client, sign_in, counselee, store
    ):
        """Inline is the *only* difference between this and the download response.

        Written out rather than compared with the download's headers, because the
        risk being guarded against is somebody adding an inline path that skips one
        of these, and a test that compares the two would pass if both lost it.
        """
        document = store()
        sign_in(counselee)

        response = client.get(preview_url(document))

        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Cache-Control"] == "no-store, max-age=0"
        assert response["Referrer-Policy"] == "no-referrer"
        assert response["Cross-Origin-Resource-Policy"] == "same-origin"

    def test_an_image_is_served_under_a_policy_that_allows_it_nothing(
        self, client, sign_in, counselee, store
    ):
        document = store(name="photo.jpg", data=jpeg_bytes())
        sign_in(counselee)

        response = client.get(preview_url(document))

        assert response.status_code == 200
        assert response["Content-Security-Policy"] == "default-src 'none'; sandbox"

    def test_a_pdf_is_not_sandboxed_because_the_viewer_needs_to_run(
        self, client, sign_in, counselee, store
    ):
        """``sandbox`` on a PDF response leaves the browser's viewer showing a blank
        page, and a viewer that shows nothing is how a feature becomes "just
        download it". The site policy from the middleware still applies."""
        document = store()
        sign_in(counselee)

        response = client.get(preview_url(document))

        assert "sandbox" not in response["Content-Security-Policy"]

    def test_text_is_given_an_encoding_rather_than_a_guess(self, client, sign_in, counselee, store):
        document = store(name="notes.txt", data="what we talked about — briefly".encode())
        sign_in(counselee)

        response = client.get(preview_url(document))

        assert response["Content-Type"] == "text/plain; charset=utf-8"

    def test_the_filename_cannot_inject_a_header(self, client, sign_in, counselee, store):
        """The name came from a browser, so it is not trusted here either."""
        document = store(name='a "quoted"; name.pdf')
        sign_in(counselee)

        disposition = client.get(preview_url(document))["Content-Disposition"]

        assert "\n" not in disposition and "\r" not in disposition
        assert disposition.count('"') == 2

    def test_a_type_that_is_not_previewable_is_a_404_rather_than_a_download(
        self, client, sign_in, counselee, store
    ):
        """Not a quiet fallback: a page that offers "View" and saves a file instead
        has told the user something untrue, and this route is never linked for a
        type the allowlist refuses."""
        document = store(name="records.docx", data=_docx_bytes())
        sign_in(counselee)

        assert client.get(preview_url(document)).status_code == 404


class TestItIsTheSameDisclosureAsADownload:
    def test_it_is_recorded(self, client, sign_in, counselee, store):
        document = store()
        sign_in(counselee)

        client.get(preview_url(document))

        assert AuditEvent.objects.filter(
            verb=AuditVerb.DOCUMENT_DOWNLOADED, actor=counselee
        ).exists()

    def test_the_other_counselee_on_a_shared_case_cannot_open_it(
        self, client, sign_in, case, make_user, store
    ):
        """The promise the product is sold on, on the newest route.

        tests/test_documents_access.py asserts it for every route that existed when
        it was written; a route added later has to be asserted where it is added.
        """
        from apps.accounts.models import Role

        ben = make_user(Role.COUNSELEE, first_name="Ben", last_name="Ashford")
        CaseMember.objects.create(case=case, counselee=ben)
        document = store(name="private-letter.pdf")
        sign_in(ben)

        assert client.get(preview_url(document)).status_code == 404

    def test_the_bookkeeper_cannot_open_it_either(self, client, sign_in, financial_admin, store):
        document = store()
        sign_in(financial_admin)

        assert client.get(preview_url(document)).status_code == 404

    def test_anonymous_is_sent_to_sign_in(self, client, store):
        response = client.get(preview_url(store()))

        assert response.status_code == 302
        assert reverse("accounts:login") in response.headers["Location"]


class TestWhatThePageOffers:
    def test_an_image_is_shown_at_full_size_rather_than_as_a_thumbnail(
        self, client, sign_in, counselee, store
    ):
        document = store(name="photo.jpg", data=jpeg_bytes())
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.pk])).content.decode()

        assert preview_url(document) in page

    def test_a_pdf_is_offered_as_a_link(self, client, sign_in, counselee, store):
        document = store()
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.pk])).content.decode()

        assert preview_url(document) in page

    def test_nothing_is_offered_for_a_type_the_browser_cannot_render(
        self, client, sign_in, counselee, store
    ):
        """A button that leads to a 404 is worse than no button."""
        document = store(name="records.docx", data=_docx_bytes())
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.pk])).content.decode()

        assert preview_url(document) not in page


def _docx_bytes() -> bytes:
    """The smallest thing the ingest pipeline will accept as a .docx."""
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()
