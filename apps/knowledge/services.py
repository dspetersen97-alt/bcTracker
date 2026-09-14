"""
Adding to the knowledge base, reading from it, and taking things off it.

The file half of a resource goes through ``apps.documents.ingest`` — size check,
extension and signature agreeing, virus scan, EXIF strip, per-file DEK — exactly as
a document and a message attachment do. Nothing about a handout justifies a second
way of writing an encrypted file, and messaging is the precedent: ``ingest`` was
split out of the documents app so that a second app could share it rather than
reimplement the order of operations.

A link-only resource touches none of that. It is a row, and the reason it is worth
saying out loud is that ``store_resource`` therefore has two shapes: with an upload
it is a small transaction around a blob, and without one it is a single insert.
"""

import logging

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.documents import ingest
from apps.documents.ingest import UploadRejected as UploadRejected  # re-exported
from apps.knowledge.models import Resource, ResourceComment, ResourceKind

logger = logging.getLogger(__name__)


def store_resource(
    *,
    contributed_by,
    title,
    summary="",
    topics="",
    kind=None,
    url="",
    upload=None,
    request=None,
):
    """Put one resource on the shelf. Returns the Resource.

    The shape of ``documents.services.store_document`` and for its reasons: validate
    and scan outside the transaction so a refusal can be recorded even though the
    write is about to be abandoned, write the row and the blob inside one, and remove
    the blob if anything after the write fails.

    ``upload`` and ``url`` are both optional and at least one is required — enforced
    by the form for a person, by a check constraint for everything else. This function
    does not enforce it a third time: it would be a duplicate of the constraint that
    could disagree with it.
    """

    def rejected_by_the_scanner(exc):
        record(
            AuditVerb.DOCUMENT_SCAN_REJECTED,
            actor=contributed_by,
            request=request,
            filename=upload.name,
            signature=exc.signature,
            knowledge_base=True,
        )

    accepted = None
    if upload is not None:
        accepted = ingest.accept(upload, on_infected=rejected_by_the_scanner)

    resource = Resource(
        contributed_by=contributed_by,
        title=title,
        summary=summary,
        topics=topics,
        kind=kind or ResourceKind.OTHER,
        url=url,
    )
    if accepted is not None:
        resource.original_filename = accepted.filename
        resource.content_type = accepted.content_type
        resource.byte_size = accepted.byte_size
        resource.sha256 = accepted.sha256
        resource.scan_status = accepted.scan_status
        resource.scan_detail = accepted.scan_detail

    written: list = []
    try:
        with transaction.atomic():
            if accepted is not None:
                # ``seal`` mints the key, unlike a Document where a field default
                # supplies it. That is the point of it being optional there: a
                # link-only resource must leave the column null, and a default would
                # put a storage key on every row whether anything was written under
                # it or not.
                blob = ingest.seal(accepted.data)
                resource.storage_key = blob.storage_key
                resource.wrapped_dek = blob.wrapped_dek
                resource.dek_nonce = blob.dek_nonce
                written.append(blob.storage_key)

            resource.save()
            # In the same transaction as the row, as an upload's is: a shelf that
            # cannot say who added something is the failure this trail prevents.
            record(
                AuditVerb.KNOWLEDGE_RESOURCE_ADDED,
                actor=contributed_by,
                target=resource,
                request=request,
                title=resource.title,
                kind=resource.kind,
                has_file=resource.has_file,
                has_link=resource.has_link,
                filename=resource.original_filename,
                **({"converted_from": accepted.converted_from} if accepted else {}),
            )
    except BaseException:
        ingest.discard(written)
        raise

    return resource


def open_resource(resource, *, actor, request=None):
    """Return a generator of plaintext frames for a resource's file.

    ``record`` rather than ``record_or_raise``, the way ``open_template`` diverges
    from ``open_document``: a resource discloses nothing about any counselee, so a
    failed audit write should not stop a counselor printing a handout for a session
    starting in five minutes. The permission is still re-checked here, because this
    is the function that turns ciphertext into plaintext and it takes nobody's word.
    """
    if not actor.has_perm("knowledge.view_resources"):
        record(
            AuditVerb.ACCESS_DENIED,
            actor=actor,
            target=resource,
            request=request,
            permission="knowledge.view_resources",
        )
        raise PermissionDenied

    if not resource.has_file:
        # A link-only resource. The view turns this into a 404; raising rather than
        # returning nothing keeps the "no file" case from looking like an empty file.
        raise FileNotFoundError(f"Resource {resource.pk} is a link, not a file")

    record(
        AuditVerb.KNOWLEDGE_RESOURCE_DOWNLOADED,
        actor=actor,
        target=resource,
        request=request,
        title=resource.title,
        filename=resource.original_filename,
    )

    return ingest.unseal(
        storage_key=resource.storage_key,
        wrapped_dek=resource.wrapped_dek,
        dek_nonce=resource.dek_nonce,
    )


def remove_resource(resource, *, actor, request=None):
    """Take a resource off the shelf without destroying it.

    Soft, like every other deletion here. Its comments go with it — they are only
    reachable through it — and both come back if somebody removed the wrong row.
    """
    resource.soft_delete(by=actor)
    record(
        AuditVerb.KNOWLEDGE_RESOURCE_REMOVED,
        actor=actor,
        target=resource,
        request=request,
        title=resource.title,
    )
    return resource


def add_comment(resource, *, author, body, request=None):
    """Leave a note on a resource for the other counselors. Returns the comment."""
    comment = ResourceComment.objects.create(resource=resource, author=author, body=body)
    record(
        AuditVerb.KNOWLEDGE_COMMENT_ADDED,
        actor=author,
        target=comment,
        request=request,
        resource_public_id=resource.public_id,
        # The count and not the words. A second copy of what somebody wrote, outside
        # the rules that govern the first, is the thing the audit trail should not be.
        characters=len(body),
    )
    return comment


def remove_comment(comment, *, actor, request=None):
    comment.soft_delete(by=actor)
    record(
        AuditVerb.KNOWLEDGE_COMMENT_REMOVED,
        actor=actor,
        target=comment,
        request=request,
        resource_public_id=comment.resource.public_id,
        # Worth recording when the remover is not the author: an administrator taking
        # down a colleague's note is exactly the action a review would ask about.
        by_the_author=comment.author_id == actor.pk,
    )
    return comment
