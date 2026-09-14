"""
Storing and reading documents.

The order of operations for an upload lives in ``documents/ingest.py``, which
messaging's attachments share, so the two cannot drift:

    1. size check          — before anything is read into memory
    2. type identification — extension *and* signature must agree
    3. virus scan          — fail closed; an infected file never reaches disk
    4. metadata strip      — EXIF and GPS out of photos, by re-encoding
    5. encrypt and store   — per-file DEK, wrapped with the master key
    6. thumbnail           — own DEK, encrypted like anything else
    7. audit               — recorded in the same transaction as the row

Steps 1–4 are ``ingest.accept``, step 5 is ``ingest.seal``, and 6–7 stay here
because a thumbnail and an audit row are things a *Document* has. Scanning
precedes storage deliberately: scanning afterwards would mean a window in which
malware sits on the volume marked "pending", and something would have to be
trusted not to serve it during that window.

Reads go through ``open_document``, which records the access with
``record_or_raise``: if we cannot log that someone read a counselee's file, we do
not serve the file. That is the one place in this application where the audit
trail is allowed to break the user's request.
"""

import hashlib
import logging
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.audit.models import AuditVerb
from apps.audit.services import record, record_or_raise
from apps.documents import crypto, images, ingest, pdfpages, storage
from apps.documents.ingest import UploadRejected as UploadRejected  # re-exported
from apps.documents.ingest import UploadTooLarge
from apps.documents.models import Document, DocumentKind, ScanStatus, Visibility

logger = logging.getLogger(__name__)

#: Kept under the name this module used before ``ingest`` was split out of it:
#: "the upload was too large" is what a *view* catches, and the view has no other
#: reason to import the ingest module. ``UploadRejected`` is imported above for the
#: same reason.
DocumentTooLarge = UploadTooLarge


def store_document(
    *,
    case,
    owner,
    upload,
    title="",
    description="",
    kind=None,
    visibility=Visibility.PRIVATE,
    booking=None,
    request=None,
):
    """Validate, scan, encrypt, and store one uploaded file. Returns the Document.

    Raises ``UploadRejected`` for anything the uploader can act on, and lets
    ``ScannerUnavailable`` propagate — that is an operational failure, not a user
    error, and it should look like one rather than being reported as a bad file.

    Validation and scanning happen *outside* the transaction, deliberately. An
    infected upload is refused by raising, and a rejection recorded inside a block
    we are about to roll back would vanish at exactly the moment somebody wants to
    read it. Only the write is atomic, where the thing being kept consistent is the
    row against the blob.

    Blobs are removed if anything after the write fails. If an *outer*
    transaction later rolls back, the row disappears and the blob does not; that
    orphan is ciphertext whose only key reference is gone, so it is unreadable
    rather than a disclosure, and a retention job can sweep it. Trying to hook
    the outer rollback instead would mean deleting files on a code path that is
    already failing.
    """

    def rejected_by_the_scanner(exc):
        record(
            AuditVerb.DOCUMENT_SCAN_REJECTED,
            actor=owner,
            target=case,
            request=request,
            case_id=case.pk,
            filename=upload.name,
            signature=exc.signature,
        )

    accepted = ingest.accept(upload, on_infected=rejected_by_the_scanner)

    document = Document(
        case=case,
        owner=owner,
        # Whichever caller passed this has already resolved the booking through
        # ``Booking.objects.for_actor`` scoped to this case; the model's ``clean``
        # says the same thing for anything that has not.
        booking=booking,
        visibility=visibility,
        kind=kind or DocumentKind.OTHER,
        title=title,
        description=description,
        original_filename=accepted.filename,
        content_type=accepted.content_type,
        byte_size=accepted.byte_size,
        sha256=accepted.sha256,
        scan_status=accepted.scan_status,
        scan_detail=accepted.scan_detail,
    )

    written: list = []
    try:
        with transaction.atomic():
            blob = ingest.seal(accepted.data, storage_key=document.storage_key)
            document.wrapped_dek = blob.wrapped_dek
            document.dek_nonce = blob.dek_nonce
            written.append(document.storage_key)

            preview = thumbnail_source(accepted.data, accepted.content_type)
            if preview:
                _attach_thumbnail(document, preview, written)

            document.save()
            # In the same transaction as the row: a document that exists without a
            # record of having arrived is the one state the trail must not allow.
            record(
                AuditVerb.DOCUMENT_UPLOADED,
                actor=owner,
                target=document,
                request=request,
                case_id=case.pk,
                filename=document.original_filename,
                content_type=document.content_type,
                byte_size=document.byte_size,
                visibility=document.visibility,
                scan_status=document.scan_status,
                booking_id=document.booking_id,
                # Only when there was a conversion. A key that is empty on every
                # other upload would be noise in every row of the trail, and this
                # one is the answer to "where did the .docx I sent go".
                **({"converted_from": accepted.converted_from} if accepted.converted_from else {}),
            )
    except BaseException:
        ingest.discard(written)
        raise

    return document


def thumbnail_source(data: bytes, content_type: str) -> bytes | None:
    """The bytes a preview should be made from, or None if there are none.

    An image is its own preview. A PDF's is its first page, rendered — which is most
    of a counseling file, and a documents page where every row but the photographs
    showed the same grey icon was a list somebody had to open one by one to find the
    intake form they would have recognised on sight.

    A Word file arrives here as the PDF it was converted to, so it gets a page image
    too, and it is the *converted* page — which is honest, since that conversion is
    what a counselor will be reading.

    Dispatched on the content type this application decided the file has, after the
    signature check and after any conversion, never on anything the browser said.
    """
    if content_type.startswith("image/"):
        return data
    if content_type == "application/pdf":
        return pdfpages.render_first_page(data)
    return None


def backfill_thumbnail(document) -> bool:
    """Store a preview for a document that has none. True if one was made.

    For files stored before their type had a renderer — which is every PDF uploaded
    before this application could render one. A management command
    (``backfill_thumbnails``) is the only caller, and that is deliberate: this
    decrypts a counselee's file and writes a new blob, which is not something a page
    load should do because somebody scrolled past a row.

    Not audited, for the reason ``open_thumbnail`` is not: nobody has seen anything.
    The operator running it is in the deployment's own logs, and no disclosure has
    taken place.
    """
    if document.has_thumbnail:
        return False

    data = b"".join(
        ingest.unseal(
            storage_key=document.storage_key,
            wrapped_dek=document.wrapped_dek,
            dek_nonce=document.dek_nonce,
        )
    )
    preview = thumbnail_source(data, document.content_type)
    if not preview:
        return False

    written: list = []
    try:
        with transaction.atomic():
            _attach_thumbnail(document, preview, written)
            if not written:
                return False
            document.save(
                update_fields=[
                    "thumbnail_key",
                    "thumbnail_wrapped_dek",
                    "thumbnail_dek_nonce",
                    "updated_at",
                ]
            )
    except BaseException:
        ingest.discard(written)
        raise
    return True


def _attach_thumbnail(document, data: bytes, written: list) -> None:
    """Generate, encrypt, and store a preview. Silent no-op if one cannot be made.

    Its own DEK, so a thumbnail and the photo it came from share no keystream, and
    so handing a preview to a caching layer some day would not hand over the
    original's key.
    """
    thumbnail = images.make_thumbnail(data)
    if not thumbnail:
        return

    document.thumbnail_key = uuid4()
    thumbnail_dek = crypto.generate_dek()
    document.thumbnail_wrapped_dek, document.thumbnail_dek_nonce = crypto.wrap_dek(
        thumbnail_dek, storage_key=document.thumbnail_key
    )
    sealed = crypto.encrypt_bytes(thumbnail, dek=thumbnail_dek, storage_key=document.thumbnail_key)
    storage.write_blob(document.thumbnail_key, lambda handle: handle.write(sealed))
    written.append(document.thumbnail_key)


def open_document(document, *, actor, request=None):
    """Return a generator of plaintext frames, after recording the access.

    ``record_or_raise`` rather than ``record``: an unlogged disclosure is exactly
    what the audit trail exists to prevent, so if the row cannot be written the
    caller gets an exception instead of the file.

    The permission is re-checked here even though the view already resolved the
    document through ``for_actor``. This is the function that turns ciphertext
    into plaintext, so it does not take anybody's word for it.
    """
    if not actor.has_perm("documents.view_document", document):
        record(
            AuditVerb.ACCESS_DENIED,
            actor=actor,
            target=document,
            request=request,
            case_id=document.case_id,
            permission="documents.view_document",
        )
        raise PermissionDenied

    record_or_raise(
        AuditVerb.DOCUMENT_DOWNLOADED,
        actor=actor,
        target=document,
        request=request,
        case_id=document.case_id,
        filename=document.original_filename,
        byte_size=document.byte_size,
    )

    return ingest.unseal(
        storage_key=document.storage_key,
        wrapped_dek=document.wrapped_dek,
        dek_nonce=document.dek_nonce,
    )


def open_thumbnail(document) -> bytes | None:
    """Decrypt a thumbnail.

    Not audited as a download: it is shown inline on a page whose view already
    recorded DOCUMENT_VIEWED, and a per-thumbnail row on a listing page would
    bury the accesses that matter.
    """
    if not document.has_thumbnail:
        return None
    dek = crypto.unwrap_dek(
        document.thumbnail_wrapped_dek,
        document.thumbnail_dek_nonce,
        storage_key=document.thumbnail_key,
    )
    with storage.open_blob(document.thumbnail_key) as handle:
        return b"".join(crypto.decrypt_stream(handle, dek=dek, storage_key=document.thumbnail_key))


def soft_delete_document(document, *, actor, request=None):
    """Withdraw a document without destroying it.

    The ciphertext stays on disk. A document someone withdrew is still part of the
    counseling record, its audit rows still point at it, and a retention policy —
    not a click — decides when it is really gone.
    """
    document.soft_delete(by=actor)
    record(
        AuditVerb.DOCUMENT_DELETED,
        actor=actor,
        target=document,
        request=request,
        case_id=document.case_id,
        filename=document.original_filename,
    )
    return document


def share_with_case(document, *, actor, request=None):
    """Promote a document to case-shared. The counselor's decision alone."""
    document.visibility = Visibility.CASE_SHARED
    document.save(update_fields=["visibility", "updated_at"])
    record(
        AuditVerb.DOCUMENT_SHARED,
        actor=actor,
        target=document,
        request=request,
        case_id=document.case_id,
        filename=document.original_filename,
    )
    return document


def unshare(document, *, actor, request=None):
    """Return a document to private.

    Honest about what this does not do: anyone who already read it still has it.
    The audit trail records who did, which is the only thing that can be offered.
    """
    document.visibility = Visibility.PRIVATE
    document.save(update_fields=["visibility", "updated_at"])
    record(
        AuditVerb.DOCUMENT_UNSHARED,
        actor=actor,
        target=document,
        request=request,
        case_id=document.case_id,
    )
    return document


def verify_integrity(document) -> bool:
    """Re-read a document and check it against the hash recorded at upload.

    Used by the backup-restore drill: proving the database restored is the easy
    half, and proving the documents still decrypt is the half that matters.
    """
    dek = crypto.unwrap_dek(
        document.wrapped_dek, document.dek_nonce, storage_key=document.storage_key
    )
    digest = hashlib.sha256()
    try:
        with storage.open_blob(document.storage_key) as handle:
            for frame in crypto.decrypt_stream(handle, dek=dek, storage_key=document.storage_key):
                digest.update(frame)
    except (crypto.DecryptionError, OSError):
        return False
    return digest.hexdigest() == document.sha256


def scan_status_is_acceptable(status) -> bool:
    return status in (ScanStatus.CLEAN, ScanStatus.SKIPPED)
