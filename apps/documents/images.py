"""
Image handling on ingest: strip the metadata, then make a thumbnail.

The EXIF strip is not a nicety. A phone photo carries GPS coordinates, and a
counselee photographing a document at home is uploading their home address
without knowing it. Nothing in this application needs that, so it is removed
before the bytes are stored — not merely hidden at render time, which would keep
it in the file and in every backup.

Re-encoding through Pillow is what does the stripping: we decode the pixels and
write a fresh file, so any tag not deliberately carried over is gone. It also
means a malformed image fails here, in a subprocess-free decoder we control the
limits of, rather than in whatever opens it later.

Thumbnails are encrypted under their own DEK and stored like any other blob. A
plaintext derivative on disk would defeat the whole scheme for exactly the files
most likely to show a face.
"""

import io
import logging

from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

#: Refuse absurd pixel counts before decoding. Pillow's own bomb check warns at
#: ~89M pixels; this is stricter because nothing here needs a large image.
MAX_PIXELS = 40_000_000

THUMBNAIL_SIZE = (320, 320)


class ImageRejected(Exception):
    """The file claims to be an image but cannot be safely decoded."""


class ImageTooLarge(ImageRejected):
    """Too many pixels to decode, whatever the file size says.

    Separate from its parent because the HEIC fallback below stores an
    undecodable file untouched, and "we have no HEIC decoder" must not become the
    route by which a decompression bomb gets stored.
    """


def _open(data: bytes) -> Image.Image:
    """Decode an image, or refuse it. The only door onto Pillow in this app.

    The dimensions are checked from the header *before* ``load()`` decodes
    anything, because the whole risk of a decompression bomb is the allocation.
    Pillow's own guard is not enough on its own: it only *warns* between its limit
    and twice its limit, so without this check a 60-megapixel upload would be
    decoded and merely leave a line in the log.

    Both refusals are distinguished, and that matters more than it looks: a bomb
    becomes ImageTooLarge, which strip_metadata re-raises, while an ordinary
    undecodable file becomes ImageRejected, which the HEIC path is allowed to
    swallow. Collapsing the two would make "we have no HEIC decoder" the route by
    which a bomb gets stored.
    """
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise ImageTooLarge(f"That image is {width}×{height}, which is larger than we accept.")
        image.load()
    except Image.DecompressionBombError as exc:
        # Pillow got there first, which it does past twice MAX_PIXELS.
        raise ImageTooLarge("That image has too many pixels to process.") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageRejected("This image could not be read.") from exc
    return image


def strip_metadata(data: bytes, *, content_type: str) -> tuple[bytes, str]:
    """Return ``(cleaned_bytes, content_type)`` with all metadata removed.

    ``exif_transpose`` is applied first, so the orientation the EXIF tag was
    describing survives as actual pixel layout — otherwise removing the tag would
    silently rotate people's photos.

    HEIC is converted to JPEG. Pillow cannot decode HEIC without the
    ``pillow-heif`` plugin, so if that is not installed the original is stored
    untouched; the file is still scanned and encrypted, it just keeps its
    metadata. The alternative — refusing iPhone photos outright — would push
    counselees back to email, which is worse for privacy than an unstripped EXIF
    block.
    """
    try:
        image = _open(data)
    except ImageTooLarge:
        raise
    except ImageRejected:
        if content_type == "image/heic":
            logger.info("Storing a HEIC upload without stripping: no HEIC decoder available")
            return data, content_type
        raise

    image = ImageOps.exif_transpose(image) or image

    target_format = "JPEG" if content_type in ("image/jpeg", "image/heic") else "PNG"
    if target_format == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    out = io.BytesIO()
    # No exif= or pnginfo= argument, so nothing is carried over. That is the point.
    image.save(out, format=target_format, quality=90, optimize=True)
    return out.getvalue(), "image/jpeg" if target_format == "JPEG" else "image/png"


def make_thumbnail(data: bytes) -> bytes | None:
    """A small JPEG preview, or None if one cannot be made.

    Returning None rather than raising: a missing thumbnail is a cosmetic
    problem, and failing an upload over it would lose the document itself.
    """
    try:
        image = _open(data)
    except ImageRejected:
        return None

    image = ImageOps.exif_transpose(image) or image
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.thumbnail(THUMBNAIL_SIZE)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=80, optimize=True)
    return out.getvalue()


def is_within_size_limit(byte_size: int) -> bool:
    return byte_size <= settings.DOCUMENT_MAX_BYTES
