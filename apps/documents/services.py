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

The template library lives at the bottom of this module, sharing all of the above.
It is here rather than in a module of its own precisely so that it cannot grow a
second way of writing an encrypted file: a template is stored by ``ingest.accept``
and ``ingest.seal`` like everything else, and ``use_template`` puts one onto a case
by handing the bytes back to ``store_document`` rather than by cloning a row.
"""

import hashlib
import logging
from uuid import uuid4

from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.db import transaction

from apps.audit.models import AuditVerb
from apps.audit.services import record, record_or_raise
from apps.documents import crypto, images, ingest, pdfpages, storage
from apps.documents.ingest import UploadRejected as UploadRejected  # re-exported
from apps.documents.ingest import UploadTooLarge
from apps.documents.models import (
    Document,
    DocumentKind,
    DocumentTemplate,
    ScanStatus,
    Visibility,
)

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


def _attach_thumbnail(row, data: bytes, written: list) -> None:
    """Generate, encrypt, and store a preview. Silent no-op if one cannot be made.

    Its own DEK, so a thumbnail and the photo it came from share no keystream, and
    so handing a preview to a caching layer some day would not hand over the
    original's key.

    ``row`` is anything carrying the three ``thumbnail_*`` fields — a Document or a
    DocumentTemplate. It is not saved here; the caller decides that, because the
    caller is the one inside the transaction.
    """
    thumbnail = images.make_thumbnail(data)
    if not thumbnail:
        return

    row.thumbnail_key = uuid4()
    thumbnail_dek = crypto.generate_dek()
    row.thumbnail_wrapped_dek, row.thumbnail_dek_nonce = crypto.wrap_dek(
        thumbnail_dek, storage_key=row.thumbnail_key
    )
    sealed = crypto.encrypt_bytes(thumbnail, dek=thumbnail_dek, storage_key=row.thumbnail_key)
    storage.write_blob(row.thumbnail_key, lambda handle: handle.write(sealed))
    written.append(row.thumbnail_key)


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


def open_thumbnail(row) -> bytes | None:
    """Decrypt a thumbnail, of a document or of a template.

    Not audited as a download: it is shown inline on a page whose view already
    recorded DOCUMENT_VIEWED, and a per-thumbnail row on a listing page would
    bury the accesses that matter.
    """
    if not row.has_thumbnail:
        return None
    dek = crypto.unwrap_dek(
        row.thumbnail_wrapped_dek,
        row.thumbnail_dek_nonce,
        storage_key=row.thumbnail_key,
    )
    with storage.open_blob(row.thumbnail_key) as handle:
        return b"".join(crypto.decrypt_stream(handle, dek=dek, storage_key=row.thumbnail_key))


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


# --- the template library -------------------------------------------------
#
# A template is stored through the same ``ingest.accept`` / ``ingest.seal`` pair a
# document is, so nothing on the volume is a special case and nothing skips the
# scanner. What is different is what happens on the way *out*: ``use_template``
# turns one into an ordinary case document rather than sharing a reference to it.
#
# That copy is the design decision worth stating, because pointing at the template
# instead would have been less code. A counselor hands a worksheet to a counselee;
# the copy on that case has to keep saying what was handed over, so an administrator
# correcting the library's intake form next spring must not silently rewrite what
# forty cases were given. Copies also mean the case document needs no special
# access rule: it is a Document like any other, with an owner, a visibility, and the
# same audit trail.


def store_template(*, uploaded_by, upload, name="", description="", kind=None, request=None):
    """Put one file in the library. Returns the DocumentTemplate.

    The shape of ``store_document`` and for its reasons: validate and scan outside
    the transaction so a refusal can be recorded, write the row and the blob inside
    one, and remove the blob if anything after the write fails.

    No ``case`` and no ``visibility``, which is what makes this a template. Who may
    read it is decided by role alone — see ``DocumentTemplateQuerySet``.
    """

    def rejected_by_the_scanner(exc):
        record(
            AuditVerb.DOCUMENT_SCAN_REJECTED,
            actor=uploaded_by,
            request=request,
            filename=upload.name,
            signature=exc.signature,
            library=True,
        )

    accepted = ingest.accept(upload, on_infected=rejected_by_the_scanner)

    template = DocumentTemplate(
        uploaded_by=uploaded_by,
        name=name or accepted.filename,
        description=description,
        kind=kind or DocumentKind.OTHER,
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
            blob = ingest.seal(accepted.data, storage_key=template.storage_key)
            template.wrapped_dek = blob.wrapped_dek
            template.dek_nonce = blob.dek_nonce
            written.append(template.storage_key)

            preview = thumbnail_source(accepted.data, accepted.content_type)
            if preview:
                _attach_thumbnail(template, preview, written)

            template.save()
            record(
                AuditVerb.DOCUMENT_TEMPLATE_ADDED,
                actor=uploaded_by,
                target=template,
                request=request,
                name=template.name,
                filename=template.original_filename,
                content_type=template.content_type,
                byte_size=template.byte_size,
                scan_status=template.scan_status,
                **({"converted_from": accepted.converted_from} if accepted.converted_from else {}),
            )
    except BaseException:
        ingest.discard(written)
        raise

    return template


def open_template(template, *, actor, request=None):
    """Return a generator of plaintext frames, after recording the download.

    ``record`` rather than ``record_or_raise``, which is the one place this diverges
    from ``open_document`` on purpose. That function refuses to serve a file it
    cannot log, because an unlogged disclosure of a counselee's paperwork is the
    thing the trail exists to prevent. A template discloses nothing about anybody —
    it is the blank form — so a failed audit write here should not stop a counselor
    printing the intake sheet for a session that starts in five minutes.

    The permission is still re-checked, for the reason ``open_document`` re-checks:
    this is the function that turns ciphertext into plaintext, and it does not take
    the view's word for anything.
    """
    if not actor.has_perm("documents.view_document_templates"):
        record(
            AuditVerb.ACCESS_DENIED,
            actor=actor,
            target=template,
            request=request,
            permission="documents.view_document_templates",
        )
        raise PermissionDenied

    record(
        AuditVerb.DOCUMENT_TEMPLATE_DOWNLOADED,
        actor=actor,
        target=template,
        request=request,
        name=template.name,
        filename=template.original_filename,
    )

    return ingest.unseal(
        storage_key=template.storage_key,
        wrapped_dek=template.wrapped_dek,
        dek_nonce=template.dek_nonce,
    )


def withdraw_template(template, *, actor, request=None):
    """Take a template off the shelf without destroying it.

    Soft, like every other deletion here. The audit rows that say this template was
    copied onto a case still point at it, and a trail that ends in a dangling id
    cannot answer what was handed over.
    """
    template.soft_delete(by=actor)
    record(
        AuditVerb.DOCUMENT_TEMPLATE_WITHDRAWN,
        actor=actor,
        target=template,
        request=request,
        name=template.name,
    )
    return template


def use_template(
    template,
    *,
    case,
    actor,
    title="",
    visibility=Visibility.CASE_SHARED,
    request=None,
):
    """Copy a template onto a case as an ordinary document. Returns the Document.

    Deliberately routed back through ``store_document`` rather than cloning the row
    and the blob. Two things follow from that, both wanted: the copy gets its own
    DEK and its own storage key, so the case document and the library file share no
    keystream and deleting one cannot orphan the other; and it goes through the
    whole ingest pipeline again — scanner included — so there is exactly one way
    bytes become a document on a case, with no second path that trusts them because
    they came from inside the building.

    Defaults to case-shared, which is the opposite of an upload's default and is the
    reason this parameter exists at all. A template arrives on a case because a
    counselor is giving it to somebody: a worksheet nobody can open is not homework.
    A counselor who wants it private says so on the form.

    ``TEMPLATE_USED`` is recorded in addition to the ``DOCUMENT_UPLOADED`` row
    ``store_document`` writes, and it is the only record of the join: the copy is
    renameable, so its title six months from now proves nothing about where it came
    from.
    """
    data = b"".join(
        ingest.unseal(
            storage_key=template.storage_key,
            wrapped_dek=template.wrapped_dek,
            dek_nonce=template.dek_nonce,
        )
    )

    document = store_document(
        case=case,
        owner=actor,
        # A ContentFile rather than a fabricated upload object: this is not an
        # upload and nothing should have to pretend otherwise. What the pipeline
        # needs from it is a name, a size, and bytes it can seek over.
        upload=ContentFile(data, name=template.original_filename),
        title=title or template.name,
        description=template.description,
        kind=template.kind,
        visibility=visibility,
        request=request,
    )
    record(
        AuditVerb.DOCUMENT_TEMPLATE_USED,
        actor=actor,
        target=template,
        request=request,
        case_id=case.pk,
        name=template.name,
        document_public_id=document.public_id,
        visibility=document.visibility,
    )
    return document
