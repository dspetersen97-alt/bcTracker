"""
Rendering the first page of a PDF, so a list of documents can be looked at.

The documents page shows a thumbnail for every photo and, before this, nothing at
all for a PDF — which is the format almost everything in a counseling file arrives
in. A row of identical file icons is a list somebody has to open one by one to find
the intake form they already recognise on sight.

PDFium is the renderer, through ``pypdfium2``. It is the engine Chrome uses to show
the same file, so a PDF that renders here renders in the viewer on the document's
own page; and it arrives as a wheel with the library bundled, unlike poppler or
LibreOffice, which are system packages measured in hundreds of megabytes and would
have to be installed in the image for a preview nobody would miss if it failed.

What is worth being explicit about is that this decodes a file somebody uploaded, in
this process, in C. Three things bound that, and they are the reason it is allowed:

  * it runs *after* ``ingest.accept`` — so after the extension and signature agree
    that this is a PDF, and after ClamAV has seen the bytes as they were sent;
  * only the first page is rendered, at a fixed scale, and the result is thrown away
    once it has been shrunk to a 320px JPEG;
  * every failure returns None. A document with no thumbnail is the state this
    module exists to improve on, so falling back to it costs nothing, and an upload
    must never be lost over a preview.
"""

import io
import logging

logger = logging.getLogger(__name__)

#: Rendering scale, in PDFium's units of 72dpi. 1.5 puts a letter page at about
#: 900px wide, which is comfortably more than the 320px the thumbnail is reduced to
#: and small enough that a 40-page report costs nothing to open at page one.
RENDER_SCALE = 1.5


def render_first_page(data: bytes) -> bytes | None:
    """The first page of ``data`` as PNG bytes, or None if it cannot be rendered.

    PNG rather than JPEG because the caller shrinks this and re-encodes it: a JPEG
    of a JPEG of a page of text is visibly worse than one round of it, and nothing
    keeps these bytes.

    Imported inside the function rather than at module scope so that a checkout
    without the wheel — or an architecture it has no build for — is a missing
    thumbnail rather than an ``ImportError`` at startup, in an application whose
    document pages otherwise work perfectly well.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover — the wheel is a pinned dependency
        logger.info("No PDF renderer available, so PDFs are stored without a thumbnail")
        return None

    try:
        # Everything happens inside the ``with``, including writing the PNG: the image
        # ``to_pil`` hands back is a view onto PDFium's own bitmap buffer, not a copy,
        # so encoding it after the document has been closed would be reading freed
        # memory — which would work almost every time.
        with pdfium.PdfDocument(data) as document:
            if len(document) == 0:
                return None
            image = document[0].render(scale=RENDER_SCALE).to_pil()
            out = io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        # Deliberately broad. This is a C library being handed a file a counselee
        # chose, and the list of ways it can complain is not ours to enumerate — an
        # encrypted PDF, a truncated one, a page whose media box is nonsense. None of
        # them is a reason to refuse a document we have already agreed to store.
        logger.info("Could not render the first page of a PDF for a thumbnail", exc_info=True)
        return None
