"""
Give already-stored documents the previews they would get if uploaded today.

Thumbnails are made on ingest, so a document only ever has one if its type had a
renderer on the day it arrived. PDFs did not until pypdfium2 was added, which means
every case file uploaded before that shows a grey icon on the documents page forever
— the feature would arrive and appear not to work, because the files people actually
have are the old ones.

This is a one-off an operator runs after deploying that change. It is safe to run
again: a document that already has a thumbnail is skipped, and one whose first page
cannot be rendered is counted and left alone rather than retried into a failure.

What it does is decrypt files, in bulk, outside any request — so it prints only
counts, never a filename or a case, and it writes no audit rows because nobody has
been shown anything. The record that it ran is the operator's shell history and the
deployment log, which is the right place for an operational task.
"""

from django.core.management.base import BaseCommand

from apps.documents import crypto, services
from apps.documents.models import Document

#: Content types that ``services.thumbnail_source`` can make a preview from. Kept
#: here rather than inferred, so a run reports "nothing to do" on the types it was
#: never going to help with instead of loading and decrypting every document to find
#: out. Images are included because a failed upload years ago, or an image stored
#: before thumbnails existed at all, is the same problem.
RENDERABLE = ("application/pdf", "image/jpeg", "image/png")


class Command(BaseCommand):
    help = "Generate missing thumbnails for documents already in the store."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many documents are missing a thumbnail, without making any.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Stop after this many documents. Rendering is the slow part, so a "
                "large store can be worked through in batches."
            ),
        )

    def handle(self, *args, **options):
        # Soft-deleted documents are excluded by the default manager, and should be:
        # they are withdrawn from the record, and a preview of one would be a new
        # plaintext derivative of a file somebody asked to take down.
        missing = Document.objects.filter(
            thumbnail_key__isnull=True, content_type__in=RENDERABLE
        ).order_by("created_at")

        if options["dry_run"]:
            self.stdout.write(f"{missing.count()} document(s) are missing a thumbnail.")
            return

        limit = options["limit"]
        made = skipped = failed = 0
        for document in missing.iterator(chunk_size=20):
            if limit is not None and made + skipped + failed >= limit:
                break
            try:
                if services.backfill_thumbnail(document):
                    made += 1
                else:
                    # No renderer managed anything with it. Logged as a skip rather
                    # than an error: pdfpages returns None for an encrypted or
                    # malformed PDF, and that is a fact about the file.
                    skipped += 1
            except (crypto.DecryptionError, OSError):
                # The blob is unreadable — a restore that missed the volume, or a
                # file removed underneath the row. Worth finishing the run and
                # reporting a count, because stopping here would hide how many.
                failed += 1
                self.stderr.write(f"Could not read document {document.public_id}")

        self.stdout.write(f"thumbnails made: {made}")
        self.stdout.write(f"no preview available: {skipped}")
        if failed:
            self.stdout.write(self.style.ERROR(f"unreadable: {failed}"))
        self.stdout.write(self.style.SUCCESS(f"{made} thumbnail(s) generated."))
