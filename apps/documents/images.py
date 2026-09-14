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

Since the re-encode has to happen anyway, it is also where an image is made to fit
a size budget — ``DOCUMENT_IMAGE_TARGET_BYTES``, a megabyte by default. A phone
photograph of a two-page consent form is eight to twelve megabytes of sensor noise
describing a sheet of white paper, and every copy of it is on the encrypted volume,
in every backup, forever. Quality is given up before pixels are, and pixels are
given up in small steps, because what these files have to survive is being read.
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

#: JPEG qualities tried, in order, before any pixels are thrown away. 85 is not
#: distinguishable from the 90 this used to store, and it is where most of the
#: saving is. 65 is the floor: below about 60 the artefacts show on the thing these
#: files usually are — a photograph of a printed page, where every letter is a
#: high-contrast edge and JPEG is at its worst.
QUALITY_STEPS = (85, 75, 65)

#: What is left of the width and height each time an image is made smaller, and how
#: many times that may happen. Small steps rather than halving, because the point at
#: which handwriting in a margin stops being legible is not one we can see from here.
#: Four scales down from a 12-megapixel phone photo is still about 1700px on the long
#: edge, which is more than any screen shows it at.
SCALE_STEP = 0.8
SCALE_ATTEMPTS = 4


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

    The result is also brought under ``DOCUMENT_IMAGE_TARGET_BYTES`` where that can
    be done without making the image unreadable; see ``_fit_to_budget``. Which is why
    the returned content type is worth using rather than assuming: a photograph
    uploaded as a PNG comes back as a JPEG.
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
    if target_format == "JPEG":
        image = _as_opaque(image)

    encoded, target_format = _fit_to_budget(image, target_format)
    return encoded, "image/jpeg" if target_format == "JPEG" else "image/png"


def _encode(image: Image.Image, target_format: str, quality: int) -> bytes:
    """One encode. No ``exif=`` or ``pnginfo=`` argument, which is the whole point."""
    out = io.BytesIO()
    image.save(out, format=target_format, quality=quality, optimize=True)
    return out.getvalue()


def _as_opaque(image: Image.Image) -> Image.Image:
    """``image`` in a mode JPEG can hold. Alpha, if any, is composited onto white.

    Pillow refuses to write RGBA as JPEG, and ``convert("RGB")`` on its own drops
    the alpha channel by discarding it — a transparent PNG becomes whatever happened
    to be in the colour channels underneath, which for anything drawn on
    transparency is black on black. Compositing first gives what the image looked
    like in the browser it was copied out of.
    """
    if image.mode in ("RGB", "L"):
        return image
    if _has_transparency(image):
        rgba = image.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, (0, 0), rgba)
        return base
    return image.convert("RGB")


def _has_transparency(image: Image.Image) -> bool:
    """Whether any pixel is not fully opaque, cheaply and without decoding again."""
    if image.mode in ("RGBA", "LA", "PA", "La"):
        return True
    # A palette image carries transparency as an index, or a whole alpha palette, in
    # ``info`` rather than in its mode.
    return "transparency" in image.info


def _fit_to_budget(image: Image.Image, target_format: str) -> tuple[bytes, str]:
    """Encode ``image`` small enough to store, returning it and the format used.

    The order in which quality is given up is the substance of this function, and it
    is ordered by what a counselor has to be able to do with the file — read it:

      1. re-encode at the highest quality. Most uploads already fit here, because
         most of an unedited phone photo's size is metadata and sensor noise.
      2. lower the JPEG quality, in the steps above. Invisible on a photograph.
      3. only then make it smaller, ``SCALE_STEP`` at a time.

    A PNG over budget with no transparency is turned into a JPEG, because it is a
    photograph that was saved wrong — a screenshot pasted into the browser, or an
    "export image" that defaulted to lossless — and PNG has no quality dial to turn
    at step 2, only pixels to throw away at step 3. One that *does* have
    transparency stays a PNG: flattening it would change what it looks like, and
    something drawn on transparency is a diagram, where losing pixels costs less
    than losing the alpha.

    If nothing fits, the smallest attempt is returned rather than raising. The budget
    is a target for storage, not a rule about what a counselee is allowed to send;
    ``DOCUMENT_MAX_BYTES`` is the rule, and it was enforced before this ran.
    """
    budget = settings.DOCUMENT_IMAGE_TARGET_BYTES

    encoded = _encode(image, target_format, QUALITY_STEPS[0])
    if len(encoded) <= budget:
        return encoded, target_format

    if target_format == "PNG" and not _has_transparency(image):
        target_format = "JPEG"
        image = _as_opaque(image)

    # Qualities are a JPEG idea; Pillow ignores the argument for PNG, so trying three
    # of them would be the same file three times.
    qualities = QUALITY_STEPS if target_format == "JPEG" else QUALITY_STEPS[:1]
    smallest = encoded

    for attempt in range(SCALE_ATTEMPTS + 1):
        # Each attempt resamples the original rather than the previous attempt's
        # output. Resampling a resample softens an image twice for the same result.
        working = image
        if attempt:
            factor = SCALE_STEP**attempt
            width, height = int(image.width * factor), int(image.height * factor)
            if width < 1 or height < 1:
                break
            working = image.resize((width, height), Image.Resampling.LANCZOS)
        for quality in qualities:
            candidate = _encode(working, target_format, quality)
            if len(candidate) <= budget:
                return candidate, target_format
            if len(candidate) < len(smallest):
                smallest = candidate

    logger.info(
        "Could not bring an image under %d bytes; storing the smallest of %d attempts (%d bytes)",
        budget,
        SCALE_ATTEMPTS + 1,
        len(smallest),
    )
    return smallest, target_format


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
