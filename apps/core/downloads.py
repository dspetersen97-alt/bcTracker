"""
Serving a decrypted file to a browser, safely, in one place.

Two apps hand plaintext back to a browser — a document from the store and a file
attached to a message — and everything dangerous about doing that is in the
response headers rather than in the bytes. Written once here so the two cannot
drift, because the version that drifts is the version that serves a counselee's
uploaded SVG inline from our own origin.

The rules, and why each one:

  * ``Content-Disposition: attachment``, always, even for an image. An HTML or SVG
    file rendered inline from our origin runs as our origin, with the session
    cookie of whoever opened it.
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


def attachment_disposition(filename: str) -> str:
    """Build a ``Content-Disposition`` that cannot be used to inject a header.

    The filename came from a browser, so it may contain quotes, semicolons, or
    newlines. RFC 5987's ``filename*`` carries the real name percent-encoded, and
    the plain ``filename`` is reduced to a safe ASCII fallback for old clients.
    """
    safe = "".join(
        character for character in filename if character.isalnum() or character in "._- "
    )
    safe = safe.strip() or "download"
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


def encrypted_file_response(frames, *, filename: str, content_type: str, byte_size: int):
    """Stream plaintext frames back as a hardened download."""
    response = StreamingHttpResponse(frames, content_type=content_type)
    response["Content-Length"] = str(byte_size)
    response["Content-Disposition"] = attachment_disposition(filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "no-store, max-age=0"
    response["Referrer-Policy"] = "no-referrer"
    return response
