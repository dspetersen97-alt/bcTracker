"""
Accepting an uploaded file, and sealing bytes onto disk.

Extracted from ``services.store_document`` when messaging grew attachments,
because the alternative was two places that each decide what order to validate,
scan, and strip in — and the second one would eventually be written by somebody
who did not know the first one scans *before* re-encoding.

Two seams, deliberately separate:

``accept(upload)`` does everything that can refuse the file — size, type, virus,
image metadata — and returns plaintext bytes plus the facts a row needs about
them. It touches no model and writes nothing, so it can run outside a
transaction, which is what lets a refusal be audited rather than rolled back.

``seal(data)`` writes one encrypted blob and hands back the key material to store
alongside it; ``unseal`` reads it back as frames. Every blob in the application
goes through these, so "what is on the volume" has one answer: AES-GCM framed
ciphertext under an opaque UUID, keyed by a DEK the database only ever holds
wrapped.

What ``accept`` deliberately does **not** do is decide anything about who may see
the result. That belongs to the model that owns it — a Document has a
``visibility``, an attachment has a conversation — and mixing the two would put a
permission rule in the module every uploader shares.
"""

import hashlib
import io
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

from django.conf import settings

from apps.documents import crypto, filetypes, images, scanning, storage

logger = logging.getLogger(__name__)


class UploadRejected(Exception):
    """The upload will not be stored, with a reason safe to show the uploader."""


class UploadTooLarge(UploadRejected):
    pass


@dataclass(frozen=True)
class AcceptedUpload:
    """An upload that has passed everything, with the plaintext to store.

    Held in memory rather than streamed to a temporary file: the size cap is a
    handful of megabytes, and a plaintext temp file is a copy of a counselee's
    disclosure sitting outside the encrypted store, which is the thing this
    application exists to avoid.
    """

    data: bytes
    filename: str
    content_type: str
    sha256: str
    scan_status: str
    scan_detail: str
    is_image: bool

    @property
    def byte_size(self) -> int:
        return len(self.data)


def accept(upload, *, max_bytes=None, on_infected=None) -> AcceptedUpload:
    """Validate, scan, and normalise one uploaded file.

    Order matters and is the reason this function exists:

        1. size          — before anything is read into memory
        2. type          — extension *and* signature must agree
        3. virus scan     — on the bytes as sent, not on what Pillow re-encoded
        4. metadata strip — EXIF and GPS out of photos

    ``on_infected`` is called with the ``InfectedFile`` before the refusal is
    raised, so each caller can record the rejection against whatever it considers
    the target — a case for a document, a conversation for an attachment — without
    this module knowing either. It is a callback rather than a return value because
    an infected file has exactly one outcome and offering the caller a choice would
    invite a caller that stores it anyway.

    Raises ``UploadRejected`` for anything the uploader can act on, and lets
    ``ScannerUnavailable`` propagate: a scanner that is down is an operational
    failure, and reporting it as a bad file would have somebody re-exporting a
    perfectly good PDF all afternoon.
    """
    limit = settings.DOCUMENT_MAX_BYTES if max_bytes is None else max_bytes

    if upload.size is not None and upload.size > limit:
        raise UploadTooLarge(
            f"That file is {upload.size // 1024 // 1024} MB. The limit is "
            f"{limit // 1024 // 1024} MB."
        )

    try:
        file_kind = filetypes.identify(upload, filename=upload.name)
    except filetypes.UnsupportedFileType as exc:
        raise UploadRejected(str(exc)) from exc

    try:
        scan_result = scanning.scan(upload)
    except scanning.InfectedFile as exc:
        if on_infected is not None:
            on_infected(exc)
        raise UploadRejected(
            "That file was rejected by the virus scanner and has not been stored."
        ) from exc

    upload.seek(0)
    data = upload.read()
    if not data:
        raise UploadRejected("That file is empty.")
    if len(data) > limit:
        # ``upload.size`` can be missing or wrong depending on the upload handler,
        # so the limit is enforced again against what was actually read.
        raise UploadTooLarge("That file is larger than the limit.")

    content_type = file_kind.content_type
    if file_kind.is_image:
        try:
            data, content_type = images.strip_metadata(data, content_type=content_type)
        except images.ImageRejected as exc:
            # These messages are written to be shown to the uploader: "too many
            # pixels" and "cannot be decoded" are different problems, and only one
            # of them is worth trying again.
            raise UploadRejected(str(exc) or "That image could not be read.") from exc

    return AcceptedUpload(
        data=data,
        filename=(upload.name or "")[:255],
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
        scan_status=scan_result.status,
        scan_detail=scan_result.detail[:200],
        is_image=file_kind.is_image,
    )


@dataclass(frozen=True)
class SealedBlob:
    """Where the ciphertext went, and what a row must keep to read it again."""

    storage_key: UUID
    wrapped_dek: bytes
    dek_nonce: bytes


def seal(data: bytes, *, storage_key: UUID | None = None) -> SealedBlob:
    """Encrypt ``data`` under a fresh DEK and write it as one blob.

    A new DEK every time, never a key derived from anything reusable, so two files
    share no keystream and a compromised key is one file.

    The caller is responsible for removing the blob if whatever it is doing fails
    afterwards — ``storage.delete_blob``. Deleting from in here would mean this
    function had to know which failures are recoverable.
    """
    storage_key = storage_key or uuid4()
    dek = crypto.generate_dek()
    wrapped_dek, dek_nonce = crypto.wrap_dek(dek, storage_key=storage_key)
    storage.write_blob(
        storage_key,
        lambda handle: crypto.encrypt_stream(
            io.BytesIO(data), handle, dek=dek, storage_key=storage_key
        ),
    )
    return SealedBlob(storage_key=storage_key, wrapped_dek=wrapped_dek, dek_nonce=dek_nonce)


def unseal(*, storage_key: UUID, wrapped_dek: bytes, dek_nonce: bytes) -> Iterator[bytes]:
    """Read a blob back as plaintext frames, closing the handle when exhausted.

    A generator rather than bytes so a download streams and a large file never
    exists in memory in the clear. The ``finally`` matters: an abandoned response —
    a counselee closing the tab mid-download — must not leave the descriptor open.
    """
    dek = crypto.unwrap_dek(wrapped_dek, dek_nonce, storage_key=storage_key)
    handle = storage.open_blob(storage_key)

    def frames():
        try:
            yield from crypto.decrypt_stream(handle, dek=dek, storage_key=storage_key)
        finally:
            handle.close()

    return frames()


def discard(storage_keys) -> None:
    """Remove blobs written by something that then failed.

    Swallows the error and logs it: the caller is already raising, and a failure to
    clean up leaves ciphertext whose key reference is gone — unreadable, and a
    retention sweep's problem rather than this request's.
    """
    for key in storage_keys:
        try:
            storage.delete_blob(key)
        except OSError:
            logger.exception("Could not remove a blob after a failed upload: %s", key)
