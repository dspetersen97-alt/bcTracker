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

The document page shows the previewable types where somebody is already standing,
in a frame of that same route, rather than sending them to a new tab. That is the
one reason ``frame-src`` and ``frame-ancestors`` are 'self' rather than 'none', so
the tests at the bottom cover both ends of it: the page frames the document, and
the response is one a browser will let be framed. tests/test_security_headers.py
holds the policy itself to exactly that much.
"""

import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import Role
from apps.audit.models import AuditEvent, AuditVerb
from apps.core.downloads import may_be_shown_inline
from apps.counseling.models import Case, CaseMember
from apps.documents import services
from apps.documents.models import Document, Visibility
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
    return reverse("documents:preview", args=[document.public_id])


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

    def test_it_may_be_framed_by_one_of_our_own_pages(self, client, sign_in, counselee, store):
        """The document page shows the PDF in a frame, and both of these headers are
        enforced on the *framed* response rather than on the page doing the framing.
        Either one left at 'none'/DENY is an empty box with nothing in the console to
        explain it, so both are asserted on the response a browser actually loads."""
        document = store()
        sign_in(counselee)

        response = client.get(preview_url(document))

        assert response["X-Frame-Options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in response["Content-Security-Policy"]

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
        type the allowlist refuses.

        A saved web page is the sharp example, which is why it is the one used here.
        It can be uploaded, and serving it inline from this origin would be running
        somebody's HTML with a counselee's session attached — so text/html is
        deliberately absent from INLINE_TYPES and this route has to refuse it.
        """
        document = store(name="devotional.html", data=b"<!doctype html><p>Day one</p>")
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

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert preview_url(document) in page

    def test_a_pdf_is_shown_on_the_page_rather_than_in_another_tab(
        self, client, sign_in, counselee, store
    ):
        """What a counselor asked for: open the document and read it, without a tab
        that has to be found again and closed."""
        document = store()
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert re.search(rf'<iframe[^>]*src="{re.escape(preview_url(document))}"', page)
        assert 'target="_blank"' not in page

    def test_the_frame_says_what_is_in_it(self, client, sign_in, counselee, store):
        """A frame with no title is "frame" in a screen reader's list of them, on a
        page whose whole subject is which document this is."""
        document = store(name="disclosure.pdf")
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        frame = re.search(r"<iframe[^>]*>", page).group(0)
        assert 'title="' in frame
        assert document.display_name in frame

    def test_text_is_shown_the_same_way(self, client, sign_in, counselee, store):
        """The other type with no thumbnail and nothing to look at otherwise."""
        document = store(name="notes.txt", data=b"what we talked about")
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert re.search(rf'<iframe[^>]*src="{re.escape(preview_url(document))}"', page)

    def test_a_pdf_is_not_reduced_to_its_thumbnail(self, client, sign_in, counselee, store):
        """The thumbnail branch comes last for a reason: a 320px picture of page one
        beside a working viewer is two answers to the same question."""
        document = store()
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert reverse("documents:thumbnail", args=[document.public_id]) not in page

    def test_the_frame_has_a_stated_height(self, client, sign_in, counselee, store):
        """A frame defaults to 150px, which is a letterbox nobody reads a report
        through, and the height cannot be an attribute or an inline style here — see
        tests/test_security_headers.py for why. So it is a class, and the stylesheet
        has to have a rule for it."""
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "static" / "css" / "bctracker.css").read_text(
            encoding="utf-8"
        )
        document = store()
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert 'class="document-viewer"' in page
        rule = re.search(r"(?m)^\.document-viewer\s*\{([^}]*)\}", css)
        assert rule, "the stylesheet has no rule for .document-viewer"
        assert "height:" in rule.group(1)

    def test_the_viewer_gets_the_wider_measure(self, client, sign_in, counselee, store):
        """A letter page inside a 44rem reading column is the document rendered
        smaller than it was printed, read by zooming in and scrolling sideways. The
        width has to come from the page, because the frame is 100% of what it is
        given — and it cannot be an inline style; see tests/test_security_headers.py.
        """
        document = store()
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert re.search(r'<main id="main" class="[^"]*\bwide\b', page)

    def test_an_image_keeps_the_reading_measure(self, client, sign_in, counselee, store):
        """The width is for the viewer, not for the page. A photograph is shown at its
        own size and the description beside it is prose."""
        document = store(name="photo.jpg", data=jpeg_bytes())
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert not re.search(r'<main id="main" class="[^"]*\bwide\b', page)

    def test_nothing_is_offered_for_a_type_the_browser_cannot_render(
        self, client, sign_in, counselee, store
    ):
        """A button that leads to a 404 is worse than no button."""
        document = store(name="budget.xlsx", data=_xlsx_bytes())
        sign_in(counselee)

        page = client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

        assert preview_url(document) not in page


class TestReadingThroughACasesDocuments:
    """The Next button, which is what turns eleven documents into one sitting.

    Without it, reading a case's file is: list, document, back, list, document, back.
    The rule it follows is that it hands over the next row of the case's own list —
    newest first, so the one uploaded just before this one — and that it is resolved
    through ``for_actor``, so it can never step onto something the reader was not going
    to be shown.
    """

    def detail_page(self, client, document):
        return client.get(reverse("documents:detail", args=[document.public_id])).content.decode()

    def test_the_next_row_of_the_list_is_offered(self, client, sign_in, counselee, store):
        older = store(name="intake.pdf")
        newer = store(name="consent.pdf")
        sign_in(counselee)

        page = self.detail_page(client, newer)

        assert reverse("documents:detail", args=[older.public_id]) in page
        assert "Next document" in page

    def test_the_last_one_offers_nothing(self, client, sign_in, counselee, store):
        """Absent rather than disabled: a control that does nothing invites the click
        that proves it does nothing."""
        oldest = store(name="intake.pdf")
        store(name="consent.pdf")
        sign_in(counselee)

        assert "Next document" not in self.detail_page(client, oldest)

    def test_it_never_steps_onto_something_the_reader_cannot_open(
        self, client, sign_in, counselee, make_user, case, store
    ):
        """A spouse's private upload sits between two of theirs in the case's history.
        Next skips it in silence — offering it and refusing it would be worse than not
        offering it, and even a gap in the sequence would say something."""
        other_counselee = make_user(Role.COUNSELEE)
        CaseMember.objects.create(case=case, counselee=other_counselee)
        mine_first = store(name="my-intake.pdf")
        store(name="theirs.pdf", owner=other_counselee)
        mine_last = store(name="my-consent.pdf")
        sign_in(counselee)

        page = self.detail_page(client, mine_last)

        assert reverse("documents:detail", args=[mine_first.public_id]) in page

    def test_a_document_on_another_case_is_never_next(
        self, client, sign_in, counselee, counselor, store
    ):
        """The sequence is one case's documents. Stepping across cases would put two
        counseling relationships in one line of clicks."""
        first = store(name="intake.pdf")
        other_case = Case.objects.create(counselor=counselor, label="Marsh — individual")
        CaseMember.objects.create(case=other_case, counselee=counselee)
        elsewhere = services.store_document(
            case=other_case,
            owner=counselee,
            upload=SimpleUploadedFile("elsewhere.pdf", PDF),
            visibility=Visibility.PRIVATE,
        )
        sign_in(counselee)

        page = self.detail_page(client, first)

        assert reverse("documents:detail", args=[elsewhere.public_id]) not in page
        assert "Next document" not in page

    def test_two_uploaded_in_the_same_instant_do_not_point_at_each_other(
        self, client, sign_in, counselee, store
    ):
        """The tie-break, and the reason it is not decoration: several documents
        uploaded in one sitting can share a timestamp exactly, and with no total order
        each of a pair is "before" the other — a Next button that returns to the
        document it was clicked from."""
        first = store(name="one.pdf")
        second = store(name="two.pdf")
        Document.objects.filter(pk__in=[first.pk, second.pk]).update(created_at=first.created_at)
        sign_in(counselee)

        pages = [self.detail_page(client, first), self.detail_page(client, second)]

        offered = [
            reverse("documents:detail", args=[d.public_id]) in page
            for d, page in zip((second, first), pages, strict=True)
        ]
        assert offered.count(True) == 1, "exactly one of the pair leads to the other"


def _xlsx_bytes() -> bytes:
    """The smallest thing the ingest pipeline will accept as a spreadsheet.

    A spreadsheet rather than a Word file, because a .docx no longer stays a .docx:
    it is converted to a PDF on the way in, and a PDF is previewable.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return buffer.getvalue()
