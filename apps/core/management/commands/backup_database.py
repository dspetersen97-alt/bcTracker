"""
The nightly encrypted database backup.

Run by the cron sidecar at 02:15 (compose/cron/bctracker.cron). The work is in
apps/core/backups.py; this is the operator's interface to it, and its job is to
be unambiguous about what happened — a backup job whose output has to be
interpreted is a backup nobody checks.

Exit status is the contract: non-zero means there is no new usable backup from
this run, which is what a monitoring check should look at.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core import backups


class Command(BaseCommand):
    help = "Write an encrypted pg_dump of the database, verify it, and prune old ones."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=None,
            help="Where to write the backup (default: BACKUP_ROOT).",
        )
        parser.add_argument(
            "--keep-days",
            type=int,
            default=None,
            help="Delete backups older than this many days (default: BACKUP_KEEP_DAYS).",
        )
        parser.add_argument(
            "--no-verify",
            action="store_true",
            help=(
                "Skip reading the backup back. Not recommended: verification is what "
                "distinguishes a backup from a file."
            ),
        )

    def handle(self, *args, **options):
        from pathlib import Path

        root = Path(options["output_dir"]) if options["output_dir"] else None
        verify_after = not options["no_verify"]

        try:
            result = backups.create(
                root=root,
                verify_after=verify_after,
                keep_days=options["keep_days"],
            )
        except Exception as exc:
            # Recorded before raising, and deliberately catching everything: a
            # backup that failed because the master key is missing or the disk is
            # full must leave a trail, and the trail is the only place anybody
            # would see it — cron output goes to the container log and scrolls
            # away.
            record(AuditVerb.BACKUP_FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
            raise CommandError(f"Backup failed: {exc}") from exc

        record(
            AuditVerb.BACKUP_CREATED,
            filename=result.dump.name,
            plaintext_bytes=result.plaintext_bytes,
            ciphertext_bytes=result.ciphertext_bytes,
            verified=result.verified,
            pruned=result.pruned,
        )

        self.stdout.write(f"wrote {result.dump} ({result.ciphertext_bytes} bytes encrypted)")
        if result.verified:
            self.stdout.write(f"verified: {result.listed_tables} table(s) in the archive")
        else:
            self.stdout.write(self.style.WARNING("not verified (--no-verify)"))
        for name in result.pruned:
            self.stdout.write(f"pruned {name}")
        self.stdout.write(
            self.style.SUCCESS(
                "Backup complete. This file is on the same host as the database — "
                "copying it off-box is a separate step (docs/restore-drill.md)."
            )
        )
