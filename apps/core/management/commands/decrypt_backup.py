"""
Read a backup back: verify it, or turn it into a dump ``pg_restore`` can take.

The other half of apps/core/backups.py, and the half that makes the nightly job
worth having. It has two modes on purpose:

  * With no ``--output``, it verifies and reports. Safe, and the thing to run on a
    schedule of its own or before a restore is attempted.
  * With ``--output``, it writes the decrypted dump. That file is a complete,
    readable copy of every counseling record this ministry holds, so the command
    treats writing one as a serious act: it refuses to overwrite, refuses to write
    into the backup or document directories, creates the file readable only by its
    owner, and records an audit row.

Restoring is deliberately not automated. ``pg_restore`` into a live database is
irreversible and situation-dependent — a drill goes into a scratch database, an
emergency does not — so the last step belongs to a person with the runbook in
front of them: docs/restore-drill.md.
"""

import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core import backups


class Command(BaseCommand):
    help = "Verify an encrypted backup, or decrypt it to a file pg_restore can read."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="The backup to read (default: the newest one in BACKUP_ROOT).",
        )
        parser.add_argument(
            "--output",
            default=None,
            help="Write the decrypted dump here instead of only verifying it.",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            dest="list_only",
            help="List the backups available and stop.",
        )

    def handle(self, *args, **options):
        if options["list_only"]:
            return self._list()

        dump = self._resolve(options["path"])
        manifest = backups.read_manifest(dump)
        self._describe(dump, manifest)

        if options["output"]:
            self._write_plaintext(dump, manifest, Path(options["output"]))
            return

        try:
            checked = backups.verify(dump)
        except backups.BackupError as exc:
            # Audited, not only printed. This command is what the weekly cron tick
            # runs to catch a backup that has rotted since it was written, and
            # nobody reads a container log; the audit trail is the surface an
            # administrator actually reviews.
            record(AuditVerb.BACKUP_FAILED, filename=dump.name, error=str(exc)[:500])
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified: {checked['plaintext_bytes']} bytes of archive, "
                f"{checked['listed_tables']} table(s) listed."
            )
        )

    def _list(self):
        available = backups.existing_backups()
        if not available:
            self.stdout.write(f"No backups in {backups.backup_root()}.")
            return
        for path in available:
            self.stdout.write(f"{path.name}  {path.stat().st_size} bytes")

    def _resolve(self, path) -> Path:
        if path:
            candidate = Path(path)
            if not candidate.exists():
                raise CommandError(f"{candidate} does not exist.")
            return candidate
        newest = backups.latest_backup()
        if newest is None:
            raise CommandError(f"No backups found in {backups.backup_root()}.")
        return newest

    def _describe(self, dump: Path, manifest: dict):
        self.stdout.write(f"backup:      {dump}")
        self.stdout.write(f"created:     {manifest.get('created_at')}")
        self.stdout.write(f"database:    {manifest.get('database')}")
        self.stdout.write(f"master key:  {manifest.get('master_key_fingerprint')}")
        documents = manifest.get("documents")
        if documents:
            self.stdout.write(
                f"documents:   {documents['files']} file(s), {documents['bytes']} bytes "
                "in the store at the time of the backup"
            )

    def _write_plaintext(self, dump: Path, manifest: dict, destination: Path):
        self._refuse_unsafe_destination(destination)
        if destination.exists():
            raise CommandError(f"{destination} already exists; refusing to overwrite it.")

        # Opened with O_EXCL and 0o600 rather than Path.write_bytes, so the file
        # cannot exist first and is never briefly world-readable.
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for frame in backups.plaintext_frames(dump, manifest):
                    handle.write(frame)
                    written += len(frame)
        except BaseException as exc:
            # A partial plaintext dump is both useless and sensitive.
            destination.unlink(missing_ok=True)
            if isinstance(exc, backups.BackupError):
                record(AuditVerb.BACKUP_FAILED, filename=dump.name, error=str(exc)[:500])
                raise CommandError(str(exc)) from exc
            raise

        record(
            AuditVerb.BACKUP_DECRYPTED,
            filename=dump.name,
            destination=str(destination),
            plaintext_bytes=written,
        )
        self.stdout.write(self.style.SUCCESS(f"Wrote {written} bytes to {destination}."))
        self.stdout.write(
            "Restore it with:  pg_restore --clean --if-exists --no-owner "
            f"--dbname <database> {destination}"
        )
        self.stdout.write(
            self.style.WARNING(
                "That file is the whole database in the clear. Delete it when the restore is done."
            )
        )

    def _refuse_unsafe_destination(self, destination: Path):
        """Keep plaintext out of the two directories that get copied elsewhere.

        The backup directory is shipped off-box and the document store sits on a
        shared volume; a plaintext dump written into either would be replicated
        as-is, which defeats the encryption entirely.
        """
        resolved = destination.resolve().parent
        for name, directory in (
            ("BACKUP_ROOT", backups.backup_root()),
            ("DOCUMENT_STORE_ROOT", Path(settings.DOCUMENT_STORE_ROOT)),
        ):
            protected = Path(directory).resolve()
            if resolved == protected or protected in resolved.parents:
                raise CommandError(
                    f"Refusing to write a plaintext dump inside {name} ({protected}). "
                    "That directory is copied off this host."
                )
