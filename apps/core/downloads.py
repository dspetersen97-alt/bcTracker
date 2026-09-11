"""
Serving a decrypted file to a browser, safely, in one place.

Two apps hand plaintext back to a browser — a document from the store and a file
attached to a message — and everything dangerous about doing that is in the
response headers rather than in the bytes. Written once here so the two cannot
drift, because the version that drifts is the version that serves a counselee's
uploaded SVG inline from our own origin.

The rules, and why each one:

  * ``Content-Disposition: attachment`` for anything that is not on the inline
    allowlist below. An HTML or SVG file rendered inline from our origin runs as
    our origin, with the session cookie of whoever opened it.
  * the content type is the one **sniffed at upload**, never one the browser or
    the uploader supplied — a client-supplied type is an instruction about how to
    execute what we hand back.
  * ``X-Content-Type-Options: nosniff``, so the browser does not overrule that.
  * ``Cache-Control: no-store``, so a counselee's file is not left in the disk
    cache of a shared church computer after they log out.
  * ``Referrer-Policy: no-referrer``, so a filename cannot leak through a
    subsequent request.

Streaming rather than buffering: a 25 MB file should not become 25 MB of process
memory per concurrent download. The cost is that a frame failing authentication
mid-stream cannot change the status code, since the response has already been
committed — which is the other reason downloads are always attachments. A
truncated attachment is a broken file the user retries, not a half-rendered page.
"""

from urllib.parse import quote

from django.http import StreamingHttpResponse


def _disposition(kind: str, filename: str) -> str:
    """Build a ``Content-Disposition`` that cannot be used to inject a header.

    The filename came from a browser, so it may contain quotes, semicolons, or
    newlines. RFC 5987's ``filename*`` carries the real name percent-encoded, and
    the plain ``filename`` is reduced to a safe ASCII fallback for old clients.
    """
    safe = "".join(
        character for character in filename if character.isalnum() or character in "._- "
    )
    safe = safe.strip() or "download"
    return f"{kind}; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


def attachment_disposition(filename: str) -> str:
    return _disposition("attachment", filename)


def inline_disposition(filename: str) -> str:
    """For the allowlisted types only — see ``may_be_shown_inline``."""
    return _disposition("inline", filename)


def _looks_like_text(head: bytes) -> bool:
    """No NUL and no stray control bytes, which is as much as a prefix can tell us.

    Not ``head.decode()``: the head is the first frame of a stream, so a valid
    UTF-8 file can be cut in the middle of a character and would fail to decode
    while being perfectly good text.
    """
    if b"\x00" in head:
        return False
    printable = bytes(range(0x20, 0x100)) + b"\t\r\n\f"
    return not head.translate(None, printable)


#: The only content types this application will ever render in a browser instead
#: of handing over as a file, each with a test on the first bytes of the
#: *plaintext*.
#:
#: Two checks, not one. The stored content type was sniffed at upload and is
#: trustworthy, but this is the function that decides whether a browser is told to
#: render something, so it looks at the bytes it is about to serve rather than at a
#: column that a future migration, import, or bug could set.
#:
#: What is deliberately absent matters more than what is here:
#:
#:   * **SVG** — an image format that is a document, with scripts and external
#:     references. Inline from our origin it is a cross-site scripting vector with
#:     a counselee's session attached. It is not on this list and must not be. It
#:     cannot be uploaded either (apps/documents/filetypes.py); both refusals are
#:     deliberate, because the day one of them is relaxed the other is the guard.
#:   * **HTML and XML** — the same, less subtly.
#:   * **Office documents and HEIC** — no browser renders them, and handing one to
#:     a plugin is what ``object-src 'none'`` exists to stop. An iPhone photo is
#:     shown through its thumbnail instead, which we re-encoded ourselves.
#:
#: PDF is here despite carrying its own scripting, because a counselor reading a
#: report is the case this exists for, and browsers render PDFs in a viewer with no
#: access to the origin that served them. Plain text is here because ``nosniff``
#: means a browser has to take "text/plain" as final and cannot decide it is HTML.
INLINE_TYPES = {
    "application/pdf": lambda head: head.startswith(b"%PDF-"),
    "image/jpeg": lambda head: head.startswith(b"\xff\xd8\xff"),
    "image/png": lambda head: head.startswith(b"\x89PNG\r\n\x1a\n"),
    "text/plain": _looks_like_text,
}


def may_be_shown_inline(content_type: str, head: bytes) -> bool:
    """Whether these bytes may be served with ``Content-Disposition: inline``.

    ``head`` is the start of the plaintext — the first decrypted frame is plenty.
    An unknown type, or a type whose bytes disagree with it, is a download.
    """
    signature = INLINE_TYPES.get(content_type)
    return bool(signature) and signature(head)


def encrypted_file_response(frames, *, filename: str, content_type: str, byte_size: int):
    """Stream plaintext frames back as a hardened download."""
    response = StreamingHttpResponse(frames, content_type=content_type)
    response["Content-Length"] = str(byte_size)
    response["Content-Disposition"] = attachment_disposition(filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store, max-age=0"
    response["Referrer-Policy"] = "no-referrer"
    return response


def inline_file_response(frames, *, filename: str, content_type: str, byte_size: int):
    """Stream plaintext frames back for the browser to render.

    Everything the download response sets is set here too. The differences are the
    disposition and the policy on the response itself:

      * an image gets ``default-src 'none'; sandbox``, the same policy the
        thumbnail view has always used. Nothing an image can contain is allowed to
        reach the network or become a document.
      * a PDF gets no policy of its own, so the middleware's site policy applies.
        ``sandbox`` on a PDF response stops the browser's built-in viewer from
        rendering it at all, and a blank page is how a feature gets replaced by
        "just download it".

    Callers must have checked ``may_be_shown_inline`` against the real bytes. This
    function trusts them, which is why the allowlist lives beside it rather than in
    whichever view happens to need it.
    """
    if content_type == "text/plain":
        # Stated rather than left to the browser's locale: a counselee's notes are
        # far more likely to be UTF-8 than Windows-1252, and a guess that goes wrong
        # renders their apostrophes as mojibake.
        content_type = "text/plain; charset=utf-8"

    response = StreamingHttpResponse(frames, content_type=content_type)
    response["Content-Length"] = str(byte_size)
    response["Content-Disposition"] = inline_disposition(filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store, max-age=0"
    response["Referrer-Policy"] = "no-referrer"
    if content_type.startswith("image/"):
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
