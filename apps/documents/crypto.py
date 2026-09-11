"""
Envelope encryption for stored documents.

Every document gets its own data key (DEK). The DEK is wrapped with the master
key from the environment and stored on the ``Document`` row; the payload is
sealed with the DEK. Consequences worth stating plainly:

  * Losing ``BCTRACKER_MASTER_KEY`` makes every document permanently
    unreadable, backups included. It must be backed up sealed and offline,
    stored separately from the ciphertext.
  * Rotating the master key means rewrapping DEKs — cheap, a few bytes per
    document — rather than re-encrypting the payloads. That is the whole reason
    for the extra layer.
  * A per-file DEK means a single leaked key exposes a single file.

Format
------

The payload is written as independently sealed 64 KiB frames rather than one
giant AES-GCM message, because a 25 MB single-shot seal has to be held whole in
memory both to write and to verify. Each frame's nonce is a 4-byte random prefix
chosen once per file, followed by an 8-byte big-endian counter, so nonces cannot
repeat under one DEK.

Framing alone would leave three attacks open, all closed by binding metadata into
each frame's AAD:

  * **reordering** — the frame index is in the AAD, so a swapped frame fails;
  * **cross-file substitution** — the document's UUID is in the AAD, so a frame
    lifted from another document fails even though it was sealed with a valid
    key;
  * **truncation** — the final frame is flagged in its own AAD, so a stream that
    stops early has no end marker and ``read()`` raises.

On-disk layout::

    magic "BCTD"  4 bytes
    version       1 byte
    nonce prefix  4 bytes
    then, repeated: frame length (4 bytes, big-endian) ‖ sealed frame

Nothing in the header is secret; it is all authenticated by being fed into each
frame's AAD via the document UUID and index, or is length information an observer
could derive from the file size anyway.
"""

import base64
import os
import struct
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

MAGIC = b"BCTD"
VERSION = 1

#: Plaintext bytes per frame. 64 KiB keeps memory flat while adding only a 16-byte
#: tag per frame — about 0.02% overhead.
FRAME_SIZE = 64 * 1024

NONCE_PREFIX_SIZE = 4
HEADER_SIZE = len(MAGIC) + 1 + NONCE_PREFIX_SIZE
LENGTH_SIZE = 4
KEY_SIZE = 32
TAG_SIZE = 16

#: Guards against a corrupt length field turning into a huge allocation.
MAX_FRAME_ON_DISK = FRAME_SIZE + TAG_SIZE + 1024


class DecryptionError(Exception):
    """The stored bytes are not what we wrote.

    Raised for a wrong key, a tampered or truncated file, a frame from a
    different document, and a header we do not recognise. Deliberately one
    exception type: to a caller they are the same event — this file cannot be
    trusted — and distinguishing them in a message would tell an attacker which
    of their guesses was closer.
    """


def master_key() -> bytes:
    """The key that wraps every DEK.

    Read on each call rather than cached at import, so a test can override it and
    so a rotation does not require a process restart to take effect.
    """
    raw = getattr(settings, "DOCUMENT_MASTER_KEY", "") or ""
    if not raw:
        raise ImproperlyConfigured(
            "BCTRACKER_MASTER_KEY is not set. Documents cannot be stored or read "
            "without it. Generate one with: "
            'python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"'
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured("BCTRACKER_MASTER_KEY is not valid base64.") from exc
    if len(key) != KEY_SIZE:
        raise ImproperlyConfigured(
            f"BCTRACKER_MASTER_KEY must decode to {KEY_SIZE} bytes, got {len(key)}."
        )
    return key


def generate_dek() -> bytes:
    return os.urandom(KEY_SIZE)


def wrap_dek(dek: bytes, *, storage_key: UUID) -> tuple[bytes, bytes]:
    """Seal a DEK under the master key. Returns ``(wrapped, nonce)``.

    The document's storage key goes in as AAD, so a wrapped DEK copied onto
    another document's row will not unwrap. Without it, swapping two rows'
    wrapped keys would be undetectable at this layer.
    """
    nonce = os.urandom(12)
    wrapped = AESGCM(master_key()).encrypt(nonce, dek, str(storage_key).encode())
    return wrapped, nonce


def unwrap_dek(wrapped: bytes, nonce: bytes, *, storage_key: UUID) -> bytes:
    try:
        return AESGCM(master_key()).decrypt(nonce, bytes(wrapped), str(storage_key).encode())
    except InvalidTag as exc:
        raise DecryptionError("The stored key could not be unwrapped.") from exc


def _frame_aad(storage_key: UUID, index: int, *, final: bool) -> bytes:
    """What each frame is bound to: this file, this position, and whether it ends it."""
    return b"|".join(
        [MAGIC, str(storage_key).encode(), str(index).encode(), b"1" if final else b"0"]
    )


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">Q", index)


def encrypt_stream(source, destination, *, dek: bytes, storage_key: UUID) -> int:
    """Encrypt ``source`` into ``destination``. Returns the plaintext byte count.

    ``source`` needs only ``read(n)`` and ``destination`` only ``write``, so this
    works on an uploaded file, a BytesIO, or an open path without either side
    knowing which.
    """
    aesgcm = AESGCM(dek)
    prefix = os.urandom(NONCE_PREFIX_SIZE)
    destination.write(MAGIC + bytes([VERSION]) + prefix)

    plaintext_bytes = 0
    index = 0
    chunk = source.read(FRAME_SIZE)
    while True:
        # Read ahead so the last frame can be marked as final while writing it.
        # Knowing where the stream ends is what makes truncation detectable.
        following = source.read(FRAME_SIZE)
        is_final = not following

        sealed = aesgcm.encrypt(
            _nonce(prefix, index),
            chunk,
            _frame_aad(storage_key, index, final=is_final),
        )
        destination.write(struct.pack(">I", len(sealed)))
        destination.write(sealed)

        plaintext_bytes += len(chunk)
        index += 1
        if is_final:
            break
        chunk = following

    return plaintext_bytes


def decrypt_stream(source, *, dek: bytes, storage_key: UUID):
    """Yield plaintext frames from ``source``.

    A generator, so a download streams rather than buffering the whole file, and
    so an unauthenticated frame stops the response mid-flight instead of being
    served. Callers must treat a partial response as a failure — see
    apps/documents/views.py, which is why downloads are always attachments.
    """
    header = source.read(HEADER_SIZE)
    if len(header) < HEADER_SIZE or header[: len(MAGIC)] != MAGIC:
        raise DecryptionError("Not a bcTracker document file.")
    if header[len(MAGIC)] != VERSION:
        raise DecryptionError(f"Unsupported document format version {header[len(MAGIC)]}.")
    prefix = header[len(MAGIC) + 1 :]

    aesgcm = AESGCM(dek)
    index = 0
    saw_final = False
    while True:
        raw_length = source.read(LENGTH_SIZE)
        if not raw_length:
            break
        if len(raw_length) < LENGTH_SIZE:
            raise DecryptionError("The file ends inside a frame header.")
        (length,) = struct.unpack(">I", raw_length)
        if length < TAG_SIZE or length > MAX_FRAME_ON_DISK:
            raise DecryptionError("Implausible frame length.")

        sealed = source.read(length)
        if len(sealed) < length:
            raise DecryptionError("The file ends inside a frame.")
        if saw_final:
            raise DecryptionError("Data after the final frame.")

        nonce = _nonce(prefix, index)
        # Try as a middle frame, then as the final one. The AAD differs by one
        # byte, so this costs a second tag check on the last frame only.
        for final in (False, True):
            try:
                plaintext = aesgcm.decrypt(
                    nonce, sealed, _frame_aad(storage_key, index, final=final)
                )
            except InvalidTag:
                continue
            saw_final = final
            break
        else:
            raise DecryptionError(f"Frame {index} failed authentication.")

        yield plaintext
        index += 1

    if not saw_final:
        # Every complete file ends with a frame flagged final. Reaching EOF
        # without one means bytes were removed from the end.
        raise DecryptionError("The file is truncated: no final frame.")


def encrypt_bytes(plaintext: bytes, *, dek: bytes, storage_key: UUID) -> bytes:
    """Convenience wrapper for small payloads — thumbnails, mostly."""
    import io

    out = io.BytesIO()
    encrypt_stream(io.BytesIO(plaintext), out, dek=dek, storage_key=storage_key)
    return out.getvalue()


def decrypt_bytes(ciphertext: bytes, *, dek: bytes, storage_key: UUID) -> bytes:
    import io

    return b"".join(decrypt_stream(io.BytesIO(ciphertext), dek=dek, storage_key=storage_key))
