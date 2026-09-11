"""
Word files, and the PDFs they are stored as.

A ``.docx`` uploaded to this application is converted on the way in and the PDF is
what lands in the encrypted store — see ``apps/documents/wordfiles.py`` for why. The
promises being tested here are the ones a counselee would notice:

  * **the words survive.** Headings, paragraphs, list items and the contents of
    tables all come across, in the order they were written, and Word's habit of
    splitting one word into several runs does not put spaces inside it.
  * **nothing in the document becomes formatting.** Text goes through reportlab's
    little HTML dialect, so a sentence containing ``<b>`` has to arrive as those four
    characters and not as a parse error or as bold text.
  * **an archive cannot make the server do arbitrary work.** The body is
    decompressed under a cap, and a document too long to draw is refused with a
    message saying what to do instead — not truncated, which would store half of
    somebody's homework and say nothing about the rest.

The PDF is read back by inflating its content streams, which is a fair bit of
machinery for a test and the alternative is asserting on a byte count. What is
drawn is the thing that matters; a test that only checks the file starts with
``%PDF`` would pass on a blank page.
"""

import base64
import io
import re
import zipfile
import zlib

import pytest

from apps.documents import wordfiles

WORD_NS = wordfiles.WORD_NS


# --- helpers ---------------------------------------------------------------


def docx(*body: str) -> bytes:
    """A real, if very small, WordprocessingML document in a zip."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{WORD_NS}"><w:body>{"".join(body)}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(wordfiles.DOCUMENT_PART, xml)
    return buffer.getvalue()


def para(*runs: str, style: str = "", bullet: bool = False) -> str:
    """One ``<w:p>``. Each argument is a run, which is how Word stores a sentence
    whose formatting changes half way through."""
    properties = ""
    if style or bullet:
        properties = (
            "<w:pPr>"
            + (f'<w:pStyle w:val="{style}"/>' if style else "")
            + ('<w:numPr><w:ilvl w:val="0"/></w:numPr>' if bullet else "")
            + "</w:pPr>"
        )
    return f"<w:p>{properties}{''.join(f'<w:r><w:t>{run}</w:t></w:r>' for run in runs)}</w:p>"


def table(*rows: tuple[str, ...]) -> str:
    def cell(text):
        return f"<w:tc>{para(text)}</w:tc>"

    return (
        "<w:tbl>"
        + "".join(f"<w:tr>{''.join(cell(t) for t in row)}</w:tr>" for row in rows)
        + "</w:tbl>"
    )


def drawn_text(pdf: bytes) -> str:
    """Everything a PDF draws, as one string.

    The content streams are ASCII85'd and deflated, in that order, so they are
    decoded in reverse. Text arrives as ``(some words) Tj``, and reportlab breaks a
    run wherever it changes font — so the words are intact but the spaces between
    the fragments are not, which is why the tests below look for words rather than
    for whole sentences.
    """
    pieces = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.S):
        raw = re.sub(rb"\s", b"", match.group(1))
        if raw.endswith(b"~>"):
            raw = base64.a85decode(raw, adobe=True)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        pieces.append(raw.decode("latin-1"))
    return "\n".join(pieces)


# --- reading the archive --------------------------------------------------


class TestReadingAWordFile:
    def test_paragraphs_come_out_in_order(self):
        blocks = wordfiles.extract(docx(para("First."), para("Second."), para("Third.")))

        assert [block.text for block in blocks] == ["First.", "Second.", "Third."]

    def test_runs_of_one_word_are_not_pulled_apart(self):
        """Word splits a word into runs wherever formatting changes. A space between
        them would be a typo this conversion introduced."""
        blocks = wordfiles.extract(docx(para("counsel", "ing", " helps")))

        assert blocks[0].text == "counseling helps"

    def test_an_empty_paragraph_is_not_a_block(self):
        """In Word an empty paragraph is spacing, and the styles already space
        things out. Kept, they would leave blank half-pages in the PDF."""
        blocks = wordfiles.extract(docx(para("Something."), para(), para("  "), para("More.")))

        assert [block.text for block in blocks] == ["Something.", "More."]

    @pytest.mark.parametrize(
        ("style", "level"),
        [
            ("Heading1", 1),
            ("Heading 2", 2),
            ("heading3", 3),
            ("Title", 1),
            ("Normal", 0),
            ("", 0),
        ],
    )
    def test_headings_are_recognised_by_their_style_name(self, style, level):
        """The name is all the body part carries — the numbering and the font live in
        a part this does not read."""
        blocks = wordfiles.extract(docx(para("Week one", style=style)))

        assert blocks[0].level == level

    def test_a_list_item_is_marked_as_one(self):
        blocks = wordfiles.extract(docx(para("Read Psalm 32", bullet=True), para("Plain")))

        assert [block.bullet for block in blocks] == [True, False]

    def test_line_breaks_and_tabs_inside_a_paragraph_are_kept(self):
        body = "<w:p><w:r><w:t>One</w:t><w:br/><w:t>Two</w:t><w:tab/><w:t>Three</w:t></w:r></w:p>"

        blocks = wordfiles.extract(docx(body))

        assert blocks[0].text == "One\nTwo Three"

    def test_a_table_comes_out_as_rows_of_cells(self):
        blocks = wordfiles.extract(docx(table(("Day", "Reading"), ("Monday", "Psalm 1"))))

        assert blocks == [wordfiles.TableBlock(rows=(("Day", "Reading"), ("Monday", "Psalm 1")))]

    def test_a_tables_paragraphs_are_not_also_listed_on_their_own(self):
        """The reason only the direct children of the body are walked. Walking the
        whole tree would list every cell twice, once in the table and once after it."""
        blocks = wordfiles.extract(docx(para("Before"), table(("Cell",)), para("After")))

        assert [type(block) for block in blocks] == [
            wordfiles.Block,
            wordfiles.TableBlock,
            wordfiles.Block,
        ]

    def test_a_document_with_no_body_is_empty_rather_than_an_error(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(wordfiles.DOCUMENT_PART, f'<w:document xmlns:w="{WORD_NS}"/>')

        assert wordfiles.extract(buffer.getvalue()) == []


class TestWhenTheFileIsNotWhatItClaims:
    def test_something_that_is_not_a_zip_is_refused(self):
        with pytest.raises(wordfiles.ConversionFailed):
            wordfiles.extract(b"PK\x03\x04 and then nonsense")

    def test_a_zip_with_no_word_document_in_it_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")

        with pytest.raises(wordfiles.ConversionFailed, match="not contain a Word document"):
            wordfiles.extract(buffer.getvalue())

    def test_malformed_xml_is_refused_rather_than_raising_from_the_parser(self):
        """An unbound namespace prefix, which is what a hand-built or truncated
        document.xml usually looks like."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(wordfiles.DOCUMENT_PART, "<w:document/>")

        with pytest.raises(wordfiles.ConversionFailed, match="could not be read"):
            wordfiles.extract(buffer.getvalue())

    def test_a_body_that_decompresses_past_the_cap_is_refused(self):
        """The shape of a zip bomb: the upload size limit bounds the *compressed*
        bytes, so the cap on the decompressed body is the only thing that bounds
        what one file can be made to cost."""
        enormous = docx(para("a" * (wordfiles.MAX_BODY_BYTES + 100)))

        assert len(enormous) < 100_000, "the point is that the archive itself is small"
        with pytest.raises(wordfiles.ConversionFailed, match="too long to convert"):
            wordfiles.extract(enormous)

    def test_a_document_of_too_many_paragraphs_is_refused_not_truncated(self):
        with pytest.raises(wordfiles.ConversionFailed, match="too long to convert"):
            wordfiles.extract(docx(*[para("Line.")] * (wordfiles.MAX_BLOCKS + 1)))


# --- drawing the PDF ------------------------------------------------------


class TestTheGeneratedPdf:
    def test_it_is_a_pdf(self):
        pdf = wordfiles.to_pdf([wordfiles.Block("Hello.")])

        assert pdf.startswith(b"%PDF-")
        assert pdf.rstrip().endswith(b"%%EOF")

    def test_the_text_is_on_the_page(self):
        pdf = wordfiles.to_pdf(
            [
                wordfiles.Block("Week one", level=1),
                wordfiles.Block("Read Psalm 32", bullet=True),
                wordfiles.Block("Anger is the presenting problem."),
                wordfiles.TableBlock(rows=(("Monday", "Psalm 1"),)),
            ]
        )
        drawn = drawn_text(pdf)

        for word in ("Week", "Psalm", "presenting", "Monday"):
            assert word in drawn, word

    def test_markup_in_the_document_stays_text(self):
        """reportlab paragraphs take a small HTML dialect, so this is the difference
        between a sentence and either bold text or a parse error."""
        pdf = wordfiles.to_pdf([wordfiles.Block("Use <b>bold</b> & mean it")])

        drawn = drawn_text(pdf)
        assert "bold" in drawn
        assert "&" in drawn
        assert "&amp;" not in drawn, "the escaping is for reportlab, not for the reader"

    def test_a_document_with_nothing_in_it_says_so(self):
        """A blank page invites "the upload lost my file". The note says what
        happened: the original had no text this conversion could keep."""
        drawn = drawn_text(wordfiles.to_pdf([]))

        assert "images" in drawn or "no text" in drawn

    def test_a_ragged_table_is_still_drawn(self):
        """Word's own tables are not always rectangular, and a merged cell arrives as
        a row one cell short."""
        pdf = wordfiles.to_pdf(
            [wordfiles.TableBlock(rows=(("Day", "Reading", "Done"), ("Monday",)))]
        )

        assert "Monday" in drawn_text(pdf)

    def test_the_only_metadata_is_the_files_own_name(self):
        """A PDF is a thing people forward. Its properties should not say more about
        where it came from than its contents do."""
        pdf = wordfiles.to_pdf([wordfiles.Block("Text.")], title="week-one")

        assert b"week-one" in pdf
        assert b"/Author ()" in pdf or b"/Author" not in pdf


class TestTheNameItIsStoredUnder:
    @pytest.mark.parametrize(
        ("uploaded", "stored"),
        [
            ("homework.docx", "homework.pdf"),
            ("Week One.DOCX", "Week One.pdf"),
            ("notes.for.week.2.docx", "notes.for.week.2.pdf"),
            ("nodots", "nodots.pdf"),
        ],
    )
    def test_the_extension_is_replaced_not_appended(self, uploaded, stored):
        """``homework.docx.pdf`` invites the reader to think both files are in there
        somewhere, and only the PDF is."""
        assert wordfiles.pdf_filename(uploaded) == stored

    def test_a_very_long_name_still_fits_the_column(self):
        assert len(wordfiles.pdf_filename("x" * 400 + ".docx")) <= 255

    def test_convert_returns_the_pdf_and_the_new_name(self):
        pdf, name = wordfiles.convert(docx(para("Homework.")), filename="week one.docx")

        assert pdf.startswith(b"%PDF-")
        assert name == "week one.pdf"
        assert "Homework." in drawn_text(pdf)
