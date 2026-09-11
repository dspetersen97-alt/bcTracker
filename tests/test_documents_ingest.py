"""
What happens to a file between the browser and the encrypted volume.

Three checks stand between a counselee's upload and the counselor who will click
it, and each is tested here on the thing it is actually defending against rather
than on a happy path:

  * **identification** — the extension is chosen by whoever uploads the file, so
    a renamed executable and a spreadsheet wearing a .docx suffix both have to be
    refused;
  * **scanning** — fail-closed, meaning a scanner that cannot be reached stops the
    upload. That is the behaviour most likely to be "fixed" by someone who does
    not know why it is there, so it is asserted explicitly;
  * **metadata stripping** — a phone photo carries the counselee's home
    coordinates, which is a disclosure they did not intend to make.

None of these tests need the database, and none of them need ClamAV: the scanner
is a stub that returns what a real one would.
"""

import io
import zipfile

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from apps.documents import filetypes, images, scanning
from tests.conftest import jpeg_bytes

# --- helpers ---------------------------------------------------------------

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
#: The leading bytes of a Windows executable. The point of the renamed-file tests.
EXECUTABLE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff"


def ooxml(marker: str) -> bytes:
    """A minimal OOXML container — a zip holding the member that names the format."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(marker, "<document/>")
    return buffer.getvalue()


DOCX = ooxml("word/document.xml")
XLSX = ooxml("xl/workbook.xml")


def word_document(text: str) -> bytes:
    """A .docx with real WordprocessingML in it, so it can actually be converted.

    ``DOCX`` above is enough to be *identified* as a Word file and no more, which is
    all the tests around identification need. Built by the module that owns the
    conversion rather than hand-rolled a second time here, where the two could drift.
    """
    from tests.test_word_to_pdf import docx, para

    return docx(para(text))


def upload(name: str, data: bytes):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, data)


def photo_with_location() -> bytes:
    """A JPEG carrying the tags a phone camera adds, GPS included."""
    exif = Image.Exif()
    exif[0x0110] = "SecretCam Model X"  # Model
    exif[0x9286] = "taken at home"  # UserComment
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (IFDRational(40), IFDRational(44), IFDRational(54, 100))
    gps[3] = "W"
    gps[4] = (IFDRational(73), IFDRational(59), IFDRational(8, 100))
    return jpeg_bytes(size=(24, 16), exif=exif)


class StubClamd:
    """Stands in for clamd's client, returning what the real one returns."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.scanned = 0

    def instream(self, stream):
        self.scanned += 1
        if self.error is not None:
            raise self.error
        return self.response


# --- identification -------------------------------------------------------


class TestIdentifyingWhatWasUploaded:
    @pytest.mark.parametrize(
        ("name", "data", "content_type"),
        [
            ("intake.pdf", PDF, "application/pdf"),
            ("photo.jpg", jpeg_bytes(), "image/jpeg"),
            ("photo.jpeg", jpeg_bytes(), "image/jpeg"),
            ("scan.png", PNG, "image/png"),
            (
                "homework.docx",
                DOCX,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                "budget.xlsx",
                XLSX,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("notes.txt", b"Week one reflections.\n", "text/plain"),
            ("devotional.html", b"<!doctype html><title>Day 1</title>", "text/html"),
            ("saved-page.htm", b"<html><body>Day 1</body></html>", "text/html"),
        ],
    )
    def test_the_formats_a_ministry_actually_needs(self, name, data, content_type):
        assert filetypes.identify(upload(name, data), filename=name).content_type == content_type

    def test_the_extension_case_does_not_matter(self):
        """Phones and Windows both hand over .JPG."""
        assert filetypes.identify(upload("PHOTO.JPG", jpeg_bytes()), filename="PHOTO.JPG")

    def test_an_executable_renamed_as_a_pdf_is_refused(self):
        """The reason the signature check exists at all.

        An allowlist on the extension alone is worthless, because the extension is
        the one piece of the upload entirely under the uploader's control.
        """
        with pytest.raises(filetypes.UnsupportedFileType):
            filetypes.identify(upload("invoice.pdf", EXECUTABLE), filename="invoice.pdf")

    def test_a_spreadsheet_renamed_as_a_document_is_refused(self):
        """Both are zips beginning PK\x03\x04, so only the member list tells them apart."""
        with pytest.raises(filetypes.UnsupportedFileType):
            filetypes.identify(upload("homework.docx", XLSX), filename="homework.docx")

    def test_an_empty_zip_wearing_a_docx_name_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("harmless.txt", "nothing")

        with pytest.raises(filetypes.UnsupportedFileType):
            filetypes.identify(upload("a.docx", buffer.getvalue()), filename="a.docx")

    @pytest.mark.parametrize("name", ["backup.zip", "run.exe", "script.sh", "photo.svg", "noext"])
    def test_types_with_no_counseling_use_are_refused_by_extension(self, name):
        """Archives hide their contents from the scanner; SVG is a script container."""
        with pytest.raises(filetypes.UnsupportedFileType):
            filetypes.identify(upload(name, PDF), filename=name)

    def test_binary_wearing_a_txt_name_is_refused(self):
        with pytest.raises(filetypes.UnsupportedFileType):
            filetypes.identify(upload("notes.txt", b"\x00\x01\x02binary"), filename="notes.txt")

    def test_utf8_text_is_still_text(self):
        """A multi-byte character straddling the sniff window must not fail the file."""
        data = ("Bénédiction — " * 200).encode("utf-8")

        assert filetypes.identify(upload("n.txt", data), filename="n.txt").content_type == (
            "text/plain"
        )

    def test_the_upload_is_left_rewound(self):
        """identify() reads the head; whatever runs next expects to read from zero."""
        handle = upload("intake.pdf", PDF)

        filetypes.identify(handle, filename="intake.pdf")

        assert handle.read() == PDF


# --- normalising ----------------------------------------------------------


class TestWhatIsStoredIsNotAlwaysWhatArrived:
    """``accept`` returns bytes rather than the upload, and this is why.

    A photo is re-encoded to lose its EXIF (below) and a Word file is replaced by the
    PDF it converts to, so that a counselor can read it in the page instead of
    downloading a .docx and opening Word — which ends with a plaintext copy of a
    counselee's disclosure in a Downloads folder.
    """

    def test_a_word_file_is_stored_as_a_pdf(self):
        from apps.documents import ingest

        accepted = ingest.accept(upload("Week one.docx", word_document("Homework.")))

        assert accepted.content_type == "application/pdf"
        assert accepted.filename == "Week one.pdf"
        assert accepted.data.startswith(b"%PDF-")

    def test_the_name_it_arrived_under_is_carried_along(self):
        """So the audit trail can answer "where did the .docx I sent go"."""
        from apps.documents import ingest

        accepted = ingest.accept(upload("Week one.docx", word_document("Homework.")))

        assert accepted.converted_from == "Week one.docx"

    def test_the_checksum_is_of_the_pdf_that_was_stored(self):
        """It identifies what a download will produce, which is the whole use of it."""
        import hashlib

        from apps.documents import ingest

        accepted = ingest.accept(upload("w.docx", word_document("Homework.")))

        assert accepted.sha256 == hashlib.sha256(accepted.data).hexdigest()

    def test_a_word_file_that_cannot_be_converted_is_refused_with_a_way_forward(self):
        from apps.documents import ingest

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")

        with pytest.raises(ingest.UploadRejected, match="saving it again"):
            ingest.accept(upload("broken.docx", buffer.getvalue()))

    @pytest.mark.parametrize(
        ("name", "data", "content_type"),
        [
            ("devotional.html", b"<!doctype html><p>Day 1</p>", "text/html"),
            ("notes.txt", b"Week one.\n", "text/plain"),
            ("intake.pdf", PDF, "application/pdf"),
        ],
    )
    def test_everything_else_is_stored_as_itself(self, name, data, content_type):
        """A saved web page in particular. It is accepted and it is never converted —
        and it is never served inline either, which is a separate decision made in
        apps/core/downloads.py and tested in tests/test_document_preview.py."""
        from apps.documents import ingest

        accepted = ingest.accept(upload(name, data))

        assert accepted.content_type == content_type
        assert accepted.data == data
        assert accepted.converted_from == ""


# --- scanning -------------------------------------------------------------


class TestScanningFailsClosed:
    @pytest.fixture(autouse=True)
    def _scanning_on(self, settings):
        settings.DOCUMENT_SCAN_ENABLED = True
        settings.DOCUMENT_SCAN_REQUIRED = True

    def test_a_clean_file_is_clean(self, monkeypatch):
        from apps.documents.models import ScanStatus

        monkeypatch.setattr(scanning, "_client", lambda: StubClamd({"stream": ("OK", None)}))

        assert scanning.scan(upload("a.pdf", PDF)).status == ScanStatus.CLEAN

    def test_a_named_signature_raises_with_the_signature(self, monkeypatch):
        """The signature is carried on the exception so the audit row can name it."""
        monkeypatch.setattr(
            scanning,
            "_client",
            lambda: StubClamd({"stream": ("FOUND", "Win.Test.EICAR_HDB-1")}),
        )

        with pytest.raises(scanning.InfectedFile) as caught:
            scanning.scan(upload("a.pdf", EXECUTABLE))

        assert caught.value.signature == "Win.Test.EICAR_HDB-1"

    def test_an_unreachable_scanner_stops_the_upload(self, monkeypatch):
        """The decision this whole module exists to make.

        Accepting an unscanned file would mean storing something nobody checked
        where a counselor will later open it. A ministry office noticing that
        uploads have started failing is the better outcome.
        """
        monkeypatch.setattr(
            scanning, "_client", lambda: StubClamd(error=ConnectionRefusedError("no daemon"))
        )

        with pytest.raises(scanning.ScannerUnavailable):
            scanning.scan(upload("a.pdf", PDF))

    def test_a_scanner_error_response_also_stops_the_upload(self, monkeypatch):
        """ERROR is not OK. Treating an unparsed answer as clean would be the bug."""
        monkeypatch.setattr(
            scanning, "_client", lambda: StubClamd({"stream": ("ERROR", "size limit exceeded")})
        )

        with pytest.raises(scanning.ScannerUnavailable):
            scanning.scan(upload("a.pdf", PDF))

    def test_a_missing_response_key_is_not_read_as_clean(self, monkeypatch):
        monkeypatch.setattr(scanning, "_client", lambda: StubClamd({}))

        with pytest.raises(scanning.ScannerUnavailable):
            scanning.scan(upload("a.pdf", PDF))

    def test_the_upload_is_rewound_afterwards(self, monkeypatch):
        """The bytes still have to be encrypted after being scanned."""
        monkeypatch.setattr(scanning, "_client", lambda: StubClamd({"stream": ("OK", None)}))
        handle = upload("a.pdf", PDF)

        scanning.scan(handle)

        assert handle.read() == PDF

    def test_an_unreachable_scanner_is_tolerated_only_when_that_is_configured(
        self, settings, monkeypatch
    ):
        """The dev-machine setting, and the one the deploy check warns about."""
        from apps.documents.models import ScanStatus

        settings.DOCUMENT_SCAN_REQUIRED = False
        monkeypatch.setattr(scanning, "_client", lambda: StubClamd(error=OSError("no daemon")))

        assert scanning.scan(upload("a.pdf", PDF)).status == ScanStatus.SKIPPED

    def test_an_infected_file_is_infected_even_when_scanning_is_optional(
        self, settings, monkeypatch
    ):
        """DOCUMENT_SCAN_REQUIRED is about availability, never about the verdict."""
        settings.DOCUMENT_SCAN_REQUIRED = False
        monkeypatch.setattr(
            scanning, "_client", lambda: StubClamd({"stream": ("FOUND", "Eicar-Test-Signature")})
        )

        with pytest.raises(scanning.InfectedFile):
            scanning.scan(upload("a.pdf", EXECUTABLE))

    def test_scanning_off_skips_without_contacting_anything(self, settings, monkeypatch):
        from apps.documents.models import ScanStatus

        settings.DOCUMENT_SCAN_ENABLED = False

        def unreachable():
            raise AssertionError("the scanner must not be contacted when it is disabled")

        monkeypatch.setattr(scanning, "_client", unreachable)

        assert scanning.scan(upload("a.pdf", PDF)).status == ScanStatus.SKIPPED


# --- photographs ----------------------------------------------------------


class TestStrippingMetadataFromPhotos:
    def test_the_gps_coordinates_are_gone(self):
        """The disclosure this exists to prevent.

        A counselee photographing their homework at the kitchen table is uploading
        their home address in the EXIF block. Nothing in this application needs
        it, so it does not get stored — not hidden at render time, which would
        leave it in the file and in every backup.
        """
        original = photo_with_location()
        assert Image.open(io.BytesIO(original)).getexif().get_ifd(0x8825), (
            "the fixture should start with GPS tags, or this test proves nothing"
        )

        cleaned, content_type = images.strip_metadata(original, content_type="image/jpeg")

        assert not Image.open(io.BytesIO(cleaned)).getexif().get_ifd(0x8825)
        assert content_type == "image/jpeg"

    def test_no_camera_or_comment_tags_survive_either(self):
        cleaned, _ = images.strip_metadata(photo_with_location(), content_type="image/jpeg")

        exif = Image.open(io.BytesIO(cleaned)).getexif()

        assert not dict(exif)
        assert b"SecretCam" not in cleaned
        assert b"taken at home" not in cleaned

    def test_the_picture_itself_still_looks_the_same(self):
        """Stripping must not become quietly destroying."""
        original = jpeg_bytes(size=(40, 24), colour=(180, 40, 40))

        cleaned, _ = images.strip_metadata(original, content_type="image/jpeg")
        image = Image.open(io.BytesIO(cleaned))

        assert image.size == (40, 24)
        red, green, blue = image.convert("RGB").getpixel((20, 12))
        assert red > 150 and green < 90 and blue < 90

    def test_orientation_survives_as_pixels(self):
        """Removing the tag must not rotate somebody's photo.

        An EXIF orientation of 6 means "display this rotated"; deleting the tag
        without applying it would leave every phone photo on its side.
        """
        exif = Image.Exif()
        exif[0x0112] = 6
        sideways = jpeg_bytes(size=(40, 20), exif=exif)

        cleaned, _ = images.strip_metadata(sideways, content_type="image/jpeg")

        assert Image.open(io.BytesIO(cleaned)).size == (20, 40)

    def test_a_png_stays_a_png(self):
        """Re-encoding must not silently flatten a screenshot's transparency."""
        buffer = io.BytesIO()
        Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(buffer, format="PNG")

        cleaned, content_type = images.strip_metadata(buffer.getvalue(), content_type="image/png")

        assert content_type == "image/png"
        assert Image.open(io.BytesIO(cleaned)).mode == "RGBA"

    def test_something_that_is_not_an_image_is_refused(self):
        with pytest.raises(images.ImageRejected):
            images.strip_metadata(EXECUTABLE, content_type="image/jpeg")

    def test_too_many_pixels_is_refused_before_it_is_decoded(self, monkeypatch):
        """A decompression bomb is an allocation attack, so the header decides.

        Pillow's own guard only warns at its limit and raises at twice it, which is
        why apps/documents/images.py checks the declared size itself.
        """
        monkeypatch.setattr(images, "MAX_PIXELS", 4)

        with pytest.raises(images.ImageTooLarge):
            images.strip_metadata(jpeg_bytes(size=(64, 64)), content_type="image/jpeg")

    def test_an_oversized_heic_is_not_saved_by_the_heic_fallback(self, monkeypatch):
        """The HEIC path stores an undecodable file untouched, on purpose.

        That concession must not become the way a bomb gets stored, so the size
        refusal is a different exception and passes straight through.
        """
        monkeypatch.setattr(images, "MAX_PIXELS", 4)

        with pytest.raises(images.ImageTooLarge):
            images.strip_metadata(jpeg_bytes(size=(64, 64)), content_type="image/heic")

    def test_an_undecodable_heic_is_stored_as_it_arrived(self):
        """Pillow needs pillow-heif for HEIC, and it may not be installed.

        Refusing iPhone photos outright would push counselees back to email, which
        is worse for privacy than an unstripped EXIF block. The file is still
        scanned and still encrypted.
        """
        heic_ish = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"

        stored, content_type = images.strip_metadata(heic_ish, content_type="image/heic")

        assert stored == heic_ish
        assert content_type == "image/heic"


class TestThumbnails:
    def test_a_thumbnail_fits_the_box(self):
        thumbnail = images.make_thumbnail(jpeg_bytes(size=(1200, 800)))

        width, height = Image.open(io.BytesIO(thumbnail)).size
        assert (width, height) <= images.THUMBNAIL_SIZE
        assert width == 320, "the long edge should be scaled to the limit"

    def test_a_thumbnail_is_a_jpeg_we_produced(self):
        """The reason the thumbnail view may serve its bytes inline.

        The content type is a fact about what Pillow wrote, not a claim carried
        over from the upload.
        """
        assert Image.open(io.BytesIO(images.make_thumbnail(PNG))).format == "JPEG"

    def test_a_thumbnail_carries_no_metadata_either(self):
        thumbnail = images.make_thumbnail(photo_with_location())

        assert not dict(Image.open(io.BytesIO(thumbnail)).getexif())

    def test_no_thumbnail_rather_than_a_failed_upload(self):
        """A missing preview is cosmetic; losing the document is not."""
        assert images.make_thumbnail(PDF) is None
