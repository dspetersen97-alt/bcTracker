"""
Turning an uploaded Word file into the PDF we keep.

Why convert at all. A ``.docx`` is a zip of XML that no browser will render, so a
counselor who wants to read a homework sheet has to download it and open Word,
which ends with a plaintext copy of a counselee's disclosure sitting in a
Downloads folder — the one thing the encrypted store exists to prevent. A PDF is
shown in the page instead (``apps.core.downloads.INLINE_TYPES``) and never leaves
the application. So the ``.docx`` is converted on the way in and the PDF is what
is stored.

What this is not
----------------

It is not a renderer. Headings, paragraphs, list items and tables come through;
fonts, images, columns, footnotes, headers and footers, and anything else about
how the page looked do not. A counseling document is words, and the words are
what has to survive — but this is a real loss and the uploader is told about it,
because somebody sending in a form with a diagram on it needs to know the diagram
did not arrive.

The alternative was LibreOffice: far better fidelity, and about half a gigabyte in
the image, a subprocess handling a counselee's document, and a dependency this
test suite could not rely on being installed on a maintainer's Windows machine.
Not worth it for text a counselor is going to read once.

Reading the archive safely
--------------------------

``word/document.xml`` decompresses to whatever it likes, so it is read through
``ZipFile.open`` with a byte cap rather than in one go: the size limit on the
upload bounds the *compressed* bytes and nothing else, which is exactly the shape
of a zip bomb. Over the cap the file is refused rather than truncated. Truncating
would store half of somebody's homework and say nothing about the other half.

``xml.etree`` is the standard library's parser and does not resolve external
entities or expand internal ones, so the usual XML attacks have nothing to work
with here. Text is escaped before it reaches reportlab, whose paragraphs take a
small HTML dialect — an unescaped ``<b>`` in a counselee's sentence would come out
as formatting at best and as a parse error at worst.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from html import escape
from xml.etree import ElementTree

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

#: The part of the archive that holds the body text. Everything else in a .docx —
#: styles, numbering, relationships, embedded images — describes how it should look.
DOCUMENT_PART = "word/document.xml"

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

#: Cap on the *decompressed* body. Eight megabytes of XML is a document of some
#: hundreds of pages; a .docx that needs more than that is not a worksheet.
MAX_BODY_BYTES = 8 * 1024 * 1024

#: Cap on how much is drawn. Bounds the work one upload can ask for, and a document
#: this long is one somebody should be exporting from Word themselves.
MAX_BLOCKS = 4000

PAGE_SIZE = LETTER
MARGIN = 0.9 * inch
FRAME_WIDTH = PAGE_SIZE[0] - 2 * MARGIN


class ConversionFailed(Exception):
    """The Word file could not be turned into a PDF. The message is shown to the
    person who uploaded it, so it says what to do next rather than what broke."""


@dataclass(frozen=True)
class Block:
    """One paragraph of the document. ``level`` is 1-6 for a heading, 0 for body."""

    text: str
    level: int = 0
    bullet: bool = False


@dataclass(frozen=True)
class TableBlock:
    """One table, as rows of already-flattened cell text."""

    rows: tuple[tuple[str, ...], ...]


def convert(data: bytes, *, filename: str) -> tuple[bytes, str]:
    """The PDF to store in place of ``data``, and the name to store it under.

    Raises ``ConversionFailed`` for anything the uploader can act on — a corrupt
    archive, a file too long to draw. Everything it raises is written to be read by
    the person who chose the file.
    """
    return to_pdf(extract(data), title=_title_from(filename)), pdf_filename(filename)


def pdf_filename(filename: str) -> str:
    """``notes.docx`` becomes ``notes.pdf``, kept inside the column's 255 characters.

    The extension is replaced rather than appended: ``notes.docx.pdf`` invites the
    reader to think both files are in there somewhere, and only the PDF is.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem[:250].rstrip() or 'document'}.pdf"


def extract(data: bytes) -> list:
    """The blocks of a ``.docx``, in the order they appear.

    Only the direct children of ``<w:body>`` are walked. Paragraphs buried in a
    content control or a text box are missed by that, which is the price of not
    walking the whole tree — walking it would list every paragraph inside every
    table twice, once on its own and once in its cell.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            with archive.open(DOCUMENT_PART) as part:
                xml = part.read(MAX_BODY_BYTES + 1)
    except KeyError as exc:
        raise ConversionFailed(
            "That file does not contain a Word document. If it was renamed, upload it "
            "with its original name."
        ) from exc
    except (zipfile.BadZipFile, OSError) as exc:
        raise ConversionFailed(
            "That Word file could not be opened. Try opening it in Word and saving it again."
        ) from exc

    if len(xml) > MAX_BODY_BYTES:
        raise ConversionFailed(_TOO_LONG)

    # noqa on the parse: S314 says to use defusedxml for untrusted XML, and the
    # attacks it means are external entities and entity expansion. ElementTree
    # resolves neither — an entity reference it has not been given is an error, and
    # it is never given any, because a DTD is not processed. Adding a dependency to
    # satisfy a rule of thumb would be the wrong way round; the byte cap above is the
    # limit that actually matters here.
    try:
        body = ElementTree.fromstring(xml).find(_tag("body"))  # noqa: S314
    except ElementTree.ParseError as exc:
        raise ConversionFailed(
            "That Word file could not be read. Try opening it in Word and saving it again."
        ) from exc
    if body is None:
        return []

    blocks: list = []
    for element in body:
        if element.tag == _tag("p"):
            block = _paragraph(element)
            if block is not None:
                blocks.append(block)
        elif element.tag == _tag("tbl"):
            blocks.append(_table(element))
        if len(blocks) > MAX_BLOCKS:
            raise ConversionFailed(_TOO_LONG)
    return blocks


def to_pdf(blocks, *, title: str = "") -> bytes:
    """Draw blocks onto pages and return the PDF bytes.

    Only the title is written into the PDF's metadata, and it is the file's own
    name — which is stored on the row anyway. No author, no ministry, nothing about
    the uploader: a PDF is a file people forward, and its properties should not say
    more than its contents do.
    """
    story = [_flowable(block) for block in blocks] or [
        Paragraph(
            "This Word file contained no text. The original may have held only "
            "images or a diagram, which this conversion does not keep.",
            _BODY,
        )
    ]
    buffer = io.BytesIO()
    SimpleDocTemplate(
        buffer,
        pagesize=PAGE_SIZE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=title,
        author="",
        subject="",
        creator="bcTracker",
    ).build(story)
    return buffer.getvalue()


# --- internals ------------------------------------------------------------

_TOO_LONG = (
    "That Word file is too long to convert. Save it as a PDF from Word — File, "
    "Save as, PDF — and upload that instead."
)

_BODY = ParagraphStyle(
    "body",
    fontName="Helvetica",
    fontSize=10.5,
    leading=14.5,
    spaceAfter=8,
)

_BULLET = ParagraphStyle("bullet", parent=_BODY, leftIndent=20, bulletIndent=6, spaceAfter=4)

#: One style per heading depth, shrinking towards body size. Bold as well as large,
#: because a heading that is only bigger is a heading nobody notices in a scan.
_HEADINGS = {
    level: ParagraphStyle(
        f"heading{level}",
        parent=_BODY,
        fontName="Helvetica-Bold",
        fontSize=size,
        leading=size * 1.3,
        spaceBefore=14 if level <= 2 else 10,
        spaceAfter=6,
    )
    for level, size in {1: 17, 2: 14, 3: 12.5, 4: 11.5, 5: 11, 6: 11}.items()
}

_TABLE_STYLE = TableStyle(
    [
        ("GRID", (0, 0), (-1, -1), 0.4, "#c9c6c0"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
)


def _tag(name: str) -> str:
    return f"{{{WORD_NS}}}{name}"


def _title_from(filename: str) -> str:
    return (filename.rsplit(".", 1)[0] if "." in filename else filename).strip()


def _paragraph(element):
    """One ``<w:p>`` as a Block, or None if there is nothing in it.

    An empty paragraph in Word is usually spacing, and spacing is what the styles
    above already do; carrying them over would leave a converted document with a
    blank half-page wherever somebody pressed Enter a few times.
    """
    text = _text_of(element)
    if not text:
        return None
    properties = element.find(_tag("pPr"))
    style = properties.find(_tag("pStyle")) if properties is not None else None
    return Block(
        text=text,
        level=_heading_level(style.get(_tag("val"), "") if style is not None else ""),
        bullet=properties is not None and properties.find(_tag("numPr")) is not None,
    )


def _heading_level(style_name: str) -> int:
    """The depth a Word style implies, or 0 for body text.

    Matched by name because that is all the body part carries — the numbering and
    the font live in ``word/styles.xml``, which this does not read. "Heading 1",
    "heading1" and "Title" all mean the same thing to a reader.
    """
    match = re.fullmatch(r"[Hh]eading\s*([1-9])", style_name.strip())
    if match:
        return min(int(match.group(1)), 6)
    return 1 if style_name.strip().lower() == "title" else 0


def _text_of(element) -> str:
    """All the text under an element, with tabs and line breaks kept.

    Runs are joined with nothing between them: Word splits a sentence into runs
    wherever formatting changes, so "counsel" and "ing" are two runs of one word and
    a space between them would be a typo this introduced.
    """
    pieces: list[str] = []
    for node in element.iter():
        if node.tag == _tag("t"):
            pieces.append(node.text or "")
        elif node.tag == _tag("tab"):
            pieces.append(" ")
        elif node.tag in (_tag("br"), _tag("cr")):
            pieces.append("\n")
    return "".join(pieces).strip()


def _table(element) -> TableBlock:
    rows = tuple(
        tuple(_text_of(cell) for cell in row.findall(_tag("tc")))
        for row in element.findall(_tag("tr"))
    )
    return TableBlock(rows=tuple(row for row in rows if row))


def _markup(text: str) -> str:
    """Escaped for reportlab's paragraph dialect, with line breaks preserved."""
    return escape(text, quote=False).replace("\n", "<br/>")


def _flowable(block):
    if isinstance(block, TableBlock):
        return _table_flowable(block)
    if block.level:
        return Paragraph(_markup(block.text), _HEADINGS[block.level])
    if block.bullet:
        return Paragraph(_markup(block.text), _BULLET, bulletText="•")
    return Paragraph(_markup(block.text), _BODY)


def _table_flowable(block: TableBlock):
    """A table drawn to the width of the frame, with every column the same width.

    Word's own column widths are in the part this does not read, and guessing from
    the content is how a table ends up with one column three characters wide. Equal
    columns are wrong in a different, predictable way, and every cell stays legible.
    """
    if not block.rows:
        return Paragraph("", _BODY)
    columns = max(len(row) for row in block.rows)
    padded = [
        [Paragraph(_markup(cell), _BODY) for cell in row] + [""] * (columns - len(row))
        for row in block.rows
    ]
    return Table(padded, colWidths=[FRAME_WIDTH / columns] * columns, style=_TABLE_STYLE)
