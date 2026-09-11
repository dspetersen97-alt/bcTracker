"""
What may be uploaded, and how we decide what a file actually is.

Both halves matter. A counselee emailing in a homework sheet needs PDFs, Word
files, saved web pages, and phone photos to work; nothing else has a reason to be
here, and an extension allowlist alone is worthless because the extension is
chosen by whoever uploads the file. So the extension must be allowed **and** the
leading bytes must match a signature for that extension.

Being accepted is not the same as being stored as itself. A Word file is converted
to a PDF on the way in — ``converts_to_pdf`` below, and apps/documents/wordfiles.py
for why — and a photo is re-encoded to drop its EXIF. This module only decides what
a file *is*.

Why a small signature table instead of libmagic
-----------------------------------------------

``python-magic`` needs the native libmagic library, which does not ship on
Windows without an abandoned binary wheel — and the tests for this project have
to run on the maintainer's machine as well as in the Linux container. libmagic
also identifies roughly two thousand formats when the question here is about
seven. A table this small is auditable in one screen, which is worth more than
breadth for a check whose whole job is to say no.

Deliberately absent from the allowlist: archives and anything executable.
Archives hide their contents from the scanner behind one more layer and invite
zip-bomb handling we have no reason to write; executables have no counseling use.
"""

import zipfile
from dataclasses import dataclass

#: Bytes read from the front of an upload to identify it. The longest signature
#: we check sits well inside this.
SNIFF_BYTES = 512


@dataclass(frozen=True)
class FileKind:
    """One accepted format."""

    content_type: str
    extensions: tuple[str, ...]
    #: Byte prefixes any of which identifies this format. Empty for text, which
    #: has no signature and is checked by decoding instead.
    signatures: tuple[bytes, ...] = ()
    #: True for the OOXML formats, which are zip containers distinguished by a
    #: path inside the archive rather than by their (identical) leading bytes.
    is_ooxml: bool = False
    #: Path that must exist inside the archive for an OOXML kind.
    ooxml_marker: str = ""
    is_image: bool = False
    #: True for a format we store as a PDF instead of as itself. Only Word files:
    #: see apps/documents/wordfiles.py for why they are converted and what that
    #: costs. The conversion happens in ``ingest.accept``, after the virus scan.
    converts_to_pdf: bool = False


KINDS: tuple[FileKind, ...] = (
    FileKind("application/pdf", (".pdf",), (b"%PDF-",)),
    FileKind("image/jpeg", (".jpg", ".jpeg"), (b"\xff\xd8\xff",), is_image=True),
    FileKind("image/png", (".png",), (b"\x89PNG\r\n\x1a\n",), is_image=True),
    # HEIC is what an iPhone produces by default, and a counselee photographing a
    # document with their phone is the common case.
    FileKind(
        "image/heic",
        (".heic", ".heif"),
        (b"ftypheic", b"ftypheix", b"ftypmif1"),
        is_image=True,
    ),
    FileKind(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        (".docx",),
        (b"PK\x03\x04",),
        is_ooxml=True,
        ooxml_marker="word/document.xml",
        converts_to_pdf=True,
    ),
    FileKind(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        (".xlsx",),
        (b"PK\x03\x04",),
        is_ooxml=True,
        ooxml_marker="xl/workbook.xml",
    ),
    # A saved web page — a devotional, an article, a form somebody printed to file.
    # Accepted because people do send them, and stored as itself: converting it would
    # need a browser engine, and there is nothing here that renders it. Note what does
    # *not* follow from accepting it — text/html is deliberately absent from
    # apps/core/downloads.py::INLINE_TYPES, so it is always handed over as a download
    # and never served for a browser to run from this origin, with a counselee's
    # session attached. Signature-free like .txt: HTML has no magic bytes, and a file
    # that is text and claims to be HTML is at worst untidy.
    FileKind("text/html", (".html", ".htm")),
    FileKind("text/plain", (".txt",)),
)

ALLOWED_EXTENSIONS = frozenset(ext for kind in KINDS for ext in kind.extensions)


class UnsupportedFileType(Exception):
    """The upload is not something this application accepts."""


def kinds_for_extension(extension: str) -> tuple[FileKind, ...]:
    extension = extension.lower()
    return tuple(kind for kind in KINDS if extension in kind.extensions)


def _matches_signature(head: bytes, kind: FileKind) -> bool:
    for signature in kind.signatures:
        if head.startswith(signature):
            return True
        # ISO-BMFF containers (HEIC) put the brand at offset 4, after the box
        # size, so the marker is looked for near the front rather than at it.
        if signature.startswith(b"ftyp") and signature in head[:32]:
            return True
    return False


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character may straddle the end of the sniffed window, so a
        # failure in the last few bytes is not evidence of anything.
        try:
            head[:-4].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


def identify(upload, *, filename: str) -> FileKind:
    """Decide what ``upload`` is, or refuse it.

    Returns the single accepted kind. Raises ``UnsupportedFileType`` when the
    extension is not on the allowlist or when the bytes disagree with it — a
    ``.pdf`` that begins ``MZ`` is refused rather than silently reclassified,
    because a mismatch is a stronger signal than either half alone.
    """
    extension = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    candidates = kinds_for_extension(extension)
    if not candidates:
        raise UnsupportedFileType(
            f"“{extension or filename}” is not a file type this system accepts."
        )

    upload.seek(0)
    head = upload.read(SNIFF_BYTES)
    upload.seek(0)

    for kind in candidates:
        if kind.is_ooxml:
            if _matches_signature(head, kind) and _ooxml_contains(upload, kind.ooxml_marker):
                return kind
        elif kind.signatures:
            if _matches_signature(head, kind):
                return kind
        elif _looks_like_text(head):
            return kind

    raise UnsupportedFileType(
        f"The contents of this file do not match a “{extension}” file. "
        "If it was renamed, upload it with its original name."
    )


def _ooxml_contains(upload, marker: str) -> bool:
    """Confirm an OOXML file really is the document type its extension claims.

    Every OOXML file begins with the same four zip bytes, so without looking
    inside, a .xlsx renamed to .docx would pass. Only the central directory is
    read — no member is extracted, so a zip bomb has nothing to expand into.
    """
    upload.seek(0)
    try:
        with zipfile.ZipFile(upload) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    finally:
        upload.seek(0)
    return marker in names
