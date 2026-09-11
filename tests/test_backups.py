"""
Encrypted database backups.

The database is the half of this system that holds names, case labels, session
times, and counselor notes. The documents volume is already ciphertext file by
file; a ``pg_dump`` is not, so the nightly job never lets a plaintext copy exist
on disk — see apps/core/backups.py.

**What is faked, and what is not.** ``pg_dump`` and ``pg_restore`` are installed
in the application image but not on a developer's machine, which talks to
Postgres in a container. A suite that shelled out for real would pass or fail
depending on whose laptop it ran on, so ``backups.spawn`` — the one seam — is
replaced here by a fake that plays both binaries. Everything on our side of that
seam is real: real envelope encryption with the real master key, real files on a
real temporary directory, and the real command classes.

That makes the interesting assertions the ones about *our* decisions:

  * the dump is encrypted before it reaches the disk, and the password never
    reaches the argument list;
  * a backup is read back before it is trusted, and one that fails is renamed out
    of the way rather than left looking usable;
  * nothing is pruned until the new backup verifies, and the newest is never
    pruned;
  * decrypting a backup — the broadest read of counselee data this system permits
    — is recorded, and refuses to write plaintext into a directory that gets
    copied off this host.
"""

import base64
import datetime as dt
import io
import json
import os
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.audit.models import AuditEvent, AuditVerb
from apps.core import backups

pytestmark = pytest.mark.django_db

#: Stand-in for a custom-format archive. Carries a table name so a test can assert
#: that the name does *not* appear in the encrypted file, and enough filler to
#: cross the 64 KiB frame boundary when a multi-frame file is wanted.
ARCHIVE_MARKER = b"accounts_user"


def archive(size: int = 4096) -> bytes:
    body = b"PGDMP" + ARCHIVE_MARKER + b"\x00counseling_case\x00"
    filler = bytes(range(256)) * ((size // 256) + 1)
    return (body + filler)[:size] if size > len(body) else body


def toc(*tables: str) -> str:
    """What ``pg_restore --list`` prints for an archive containing ``tables``."""
    tables = tables or ("accounts_user", "counseling_case", "documents_document")
    header = [
        ";",
        "; Archive created at 2026-09-10 02:15:00 UTC",
        ";     dbname: bctracker",
        ";     Dumped by pg_dump version: 17.6",
        ";",
    ]
    entries = [
        f"{3000 + index}; 0 16400 TABLE DATA public {table} bctracker"
        for index, table in enumerate(tables)
    ]
    return "\n".join([*header, *entries]) + "\n"


class FakeProcess:
    def __init__(self, *, stdout=None, stdin=None, status=0):
        self.stdout = stdout
        self.stdin = stdin
        self._status = status

    def wait(self):
        return self._status


class Sink:
    """Stands in for a child's stdin pipe.

    ``fails_after`` makes it behave like ``pg_restore``, which stops reading once
    it has the table of contents and leaves the writer with a broken pipe.
    """

    def __init__(self, *, fails_after=None):
        self.received = bytearray()
        self.fails_after = fails_after
        self.closed = False

    def write(self, data):
        if self.fails_after is not None and len(self.received) >= self.fails_after:
            raise BrokenPipeError(32, "Broken pipe")
        self.received += data
        return len(data)

    def close(self):
        self.closed = True


class FakePostgres:
    """Plays both binaries, and records how it was called."""

    def __init__(self):
        self.dump = archive()
        self.dump_status = 0
        self.dump_stderr = b""
        self.listing = toc()
        self.restore_status = 0
        self.restore_stderr = b""
        self.restore_stdin_fails_after = None
        self.calls = []
        self.envs = []

    def __call__(self, argv, *, env=None, stdin=None, stdout=None, stderr=None):
        from django.conf import settings

        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))

        if argv[0] == settings.PG_DUMP_PATH:
            if self.dump_stderr:
                stderr.write(self.dump_stderr)
            return FakeProcess(stdout=io.BytesIO(self.dump), status=self.dump_status)

        if self.restore_stderr:
            stderr.write(self.restore_stderr)
        if self.listing:
            stdout.write(self.listing.encode())
        sink = Sink(fails_after=self.restore_stdin_fails_after)
        return FakeProcess(stdin=sink, status=self.restore_status)

    @property
    def dump_calls(self):
        from django.conf import settings

        return [call for call in self.calls if call[0] == settings.PG_DUMP_PATH]

    @property
    def restore_calls(self):
        from django.conf import settings

        return [call for call in self.calls if call[0] == settings.PG_RESTORE_PATH]


@pytest.fixture
def postgres(monkeypatch):
    fake = FakePostgres()
    monkeypatch.setattr(backups, "spawn", fake)
    return fake


@pytest.fixture
def backup_root(settings, tmp_path):
    """Point BACKUP_ROOT at a per-test directory that does not exist yet.

    Left uncreated so the tests exercise the same first run a real deployment has.
    """
    root = tmp_path / "backups"
    settings.BACKUP_ROOT = root
    return root


def aged_backup(root: Path, *, days: int, now=None) -> Path:
    """A backup file dated ``days`` ago, for the pruning tests.

    Only the name matters: pruning reads the timestamp out of it, so nothing has
    to be encrypted to test which files are chosen.
    """
    from django.utils import timezone

    now = now or timezone.now()
    root.mkdir(parents=True, exist_ok=True)
    stamp = (now - dt.timedelta(days=days)).strftime(backups.STAMP_FORMAT)
    path = root / f"bctracker-{stamp}{backups.DUMP_SUFFIX}"
    path.write_bytes(b"not really a backup")
    backups.manifest_path(path).write_text("{}")
    return path


# --- how pg_dump is called ------------------------------------------------


class TestHowPgDumpIsInvoked:
    def test_the_archive_format_and_ownership_flags_are_set(self, backup_root, postgres):
        backups.create()

        argv = postgres.dump_calls[0]
        assert "--format=custom" in argv
        assert "--no-owner" in argv
        assert "--no-privileges" in argv

    def test_it_never_waits_for_a_password_prompt(self, backup_root, postgres):
        """A cron job has no terminal. Without --no-password a misconfigured
        password produces a process that hangs forever instead of a backup that
        fails, and the difference is a month of missing backups nobody noticed."""
        backups.create()

        assert "--no-password" in postgres.dump_calls[0]

    def test_it_dumps_the_configured_database(self, backup_root, postgres, settings):
        backups.create()

        argv = postgres.dump_calls[0]
        database = settings.DATABASES["default"]
        assert argv[argv.index("--dbname") + 1] == database["NAME"]
        assert argv[argv.index("--host") + 1] == str(database["HOST"])

    def test_the_password_goes_in_the_environment_not_the_arguments(
        self, backup_root, postgres, monkeypatch, settings
    ):
        """Anything else on the host can read a process's command line."""
        monkeypatch.setitem(settings.DATABASES["default"], "PASSWORD", "not-a-real-password")

        backups.create()

        assert postgres.envs[0]["PGPASSWORD"] == "not-a-real-password"
        assert "not-a-real-password" not in " ".join(postgres.dump_calls[0])

    def test_an_inherited_password_is_cleared_when_there_is_none_configured(
        self, backup_root, postgres, monkeypatch, settings
    ):
        """Otherwise a stray PGPASSWORD in the container environment would decide
        which credentials the backup uses."""
        monkeypatch.setitem(settings.DATABASES["default"], "PASSWORD", "")
        monkeypatch.setenv("PGPASSWORD", "left-over-from-something-else")

        backups.create()

        assert "PGPASSWORD" not in postgres.envs[0]


# --- what lands on disk ---------------------------------------------------


class TestWhatIsWritten:
    def test_it_writes_a_dump_and_a_manifest(self, backup_root, postgres):
        result = backups.create()

        assert result.dump.exists()
        assert result.manifest.exists()
        assert result.dump.name.endswith(".dump.enc")
        assert result.manifest.name == result.dump.name + ".json"

    def test_the_directory_is_created_if_it_is_missing(self, backup_root, postgres):
        assert not backup_root.exists()

        backups.create()

        assert backup_root.is_dir()

    def test_the_dump_on_disk_is_encrypted(self, backup_root, postgres):
        """The assertion that matters most in this file. A backup is the copy that
        leaves the building."""
        result = backups.create()

        stored = result.dump.read_bytes()
        assert ARCHIVE_MARKER not in stored
        assert postgres.dump not in stored

    def test_it_decrypts_back_to_exactly_what_pg_dump_produced(self, backup_root, postgres):
        postgres.dump = archive(200_000)  # several frames

        result = backups.create()

        recovered = b"".join(backups.plaintext_frames(result.dump))
        assert recovered == postgres.dump

    def test_the_manifest_records_what_a_restore_needs(self, backup_root, postgres):
        result = backups.create()

        manifest = json.loads(result.manifest.read_text())
        assert manifest["format"] == backups.MANIFEST_FORMAT
        assert manifest["ciphertext_bytes"] == result.dump.stat().st_size
        assert manifest["plaintext_bytes"] == len(postgres.dump)
        assert manifest["master_key_fingerprint"] == backups.key_fingerprint()
        assert base64.b64decode(manifest["wrapped_dek"], validate=True)
        assert manifest["migrations"]["accounts"]

    def test_the_manifest_holds_no_usable_key(self, backup_root, postgres, settings):
        """The manifest sits beside the ciphertext and travels with it, so what it
        carries has to be worthless on its own: a wrapped key, and a fingerprint
        that identifies the master key without being derived from it in a way that
        can be run backwards."""
        result = backups.create()

        manifest = json.loads(result.manifest.read_text())
        assert settings.DOCUMENT_MASTER_KEY not in result.manifest.read_text()
        assert base64.b64decode(manifest["wrapped_dek"]) != base64.b64decode(
            settings.DOCUMENT_MASTER_KEY
        )

    def test_the_manifest_fingerprints_the_document_store(self, backup_root, postgres, settings):
        """The database and the document volume are two backups, and a restore that
        recovers one without the other is not a recovery. The count is how a drill
        notices."""
        store = Path(settings.DOCUMENT_STORE_ROOT)
        (store / "ab").mkdir(parents=True)
        (store / "ab" / "one").write_bytes(b"x" * 10)
        (store / "ab" / "two").write_bytes(b"y" * 5)

        result = backups.create()

        documents = json.loads(result.manifest.read_text())["documents"]
        assert documents == {"files": 2, "bytes": 15}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_nothing_it_writes_is_readable_by_other_accounts(self, backup_root, postgres):
        result = backups.create()

        assert backup_root.stat().st_mode & 0o777 == 0o700
        assert result.dump.stat().st_mode & 0o777 == 0o600
        assert result.manifest.stat().st_mode & 0o777 == 0o600

    def test_two_runs_in_the_same_second_do_not_overwrite_each_other(self, backup_root, postgres):
        from django.utils import timezone

        now = timezone.now()
        backups.create(now=now)

        with pytest.raises(backups.BackupError, match="already exists"):
            backups.create(now=now)


# --- failures -------------------------------------------------------------


class TestWhenTheDumpFails:
    def test_a_non_zero_exit_is_an_error_carrying_what_it_said(self, backup_root, postgres):
        postgres.dump_status = 1
        postgres.dump_stderr = b"pg_dump: error: connection to server failed"

        with pytest.raises(backups.BackupError, match="connection to server failed"):
            backups.create()

    def test_a_failed_dump_leaves_nothing_behind(self, backup_root, postgres):
        """Not even a partial file. A half-written dump in the backup directory is
        the thing somebody reaches for at the worst moment."""
        postgres.dump_status = 1

        with pytest.raises(backups.BackupError):
            backups.create()

        assert list(backup_root.iterdir()) == []

    def test_a_dump_that_produced_nothing_is_refused(self, backup_root, postgres):
        """pg_dump exiting zero having written no output is the failure that looks
        most like success."""
        postgres.dump = b""

        with pytest.raises(backups.BackupError, match="no output"):
            backups.create()

        assert list(backup_root.iterdir()) == []

    def test_a_missing_master_key_stops_it_before_the_database_is_read(
        self, backup_root, postgres, settings
    ):
        """Deliberately checked first. Reading the whole database out and then
        discovering there is no key to seal it with would leave the plaintext
        somewhere."""
        from django.core.exceptions import ImproperlyConfigured

        settings.DOCUMENT_MASTER_KEY = ""

        with pytest.raises(ImproperlyConfigured):
            backups.create()

        assert postgres.calls == []


# --- verification ---------------------------------------------------------


class TestVerification:
    def test_a_good_backup_verifies(self, backup_root, postgres):
        result = backups.create()

        assert result.verified
        assert result.listed_tables == 3

    def test_verification_reads_the_whole_file_back(self, backup_root, postgres):
        """Which is what makes it verification rather than a size check: every
        frame is authenticated with the key just used to write it."""
        postgres.dump = archive(200_000)

        checked = backups.verify(backups.create().dump)

        assert checked["plaintext_bytes"] == 200_000

    def test_a_flipped_byte_is_caught(self, backup_root, postgres):
        result = backups.create()
        stored = bytearray(result.dump.read_bytes())
        stored[-1] ^= 0x01
        result.dump.write_bytes(stored)

        with pytest.raises(backups.BackupError, match="could not be decrypted"):
            backups.verify(result.dump)

    def test_a_truncated_file_is_caught(self, backup_root, postgres):
        """The failure that a checksum over a growing file would miss and that a
        copy interrupted halfway actually produces."""
        result = backups.create()
        stored = result.dump.read_bytes()
        result.dump.write_bytes(stored[: len(stored) // 2])

        with pytest.raises(backups.BackupError, match="could not be decrypted"):
            backups.verify(result.dump)

    def test_a_backup_sealed_with_another_key_says_so_plainly(
        self, backup_root, postgres, settings
    ):
        """The message names both fingerprints. During a restore, "wrong key" and
        "corrupt file" call for completely different next steps."""
        result = backups.create()
        settings.DOCUMENT_MASTER_KEY = base64.b64encode(b"a" * 32).decode()

        with pytest.raises(backups.BackupError, match="different BCTRACKER_MASTER_KEY"):
            backups.verify(result.dump)

    def test_a_missing_manifest_is_an_error_that_explains_itself(self, backup_root, postgres):
        result = backups.create()
        result.manifest.unlink()

        with pytest.raises(backups.BackupError, match="wrapped key"):
            backups.verify(result.dump)

    def test_a_manifest_from_a_future_format_is_refused(self, backup_root, postgres):
        result = backups.create()
        manifest = json.loads(result.manifest.read_text())
        manifest["format"] = "bctracker-backup/99"
        result.manifest.write_text(json.dumps(manifest))

        with pytest.raises(backups.BackupError, match="format"):
            backups.verify(result.dump)

    def test_a_digest_that_no_longer_matches_is_reported(self, backup_root, postgres):
        result = backups.create()
        manifest = json.loads(result.manifest.read_text())
        manifest["sha256"] = "0" * 64
        result.manifest.write_text(json.dumps(manifest))

        with pytest.raises(backups.BackupError, match="digest"):
            backups.verify(result.dump)

    def test_an_archive_pg_restore_cannot_read_is_reported(self, backup_root, postgres):
        postgres.restore_status = 1
        postgres.restore_stderr = b"pg_restore: error: did not find magic string"
        postgres.listing = ""

        with pytest.raises(backups.BackupError, match="did not find magic string"):
            backups.create()

    def test_an_archive_of_something_else_is_refused(self, backup_root, postgres):
        """A readable archive is not the same as a backup of this application. This
        is the check that catches a dump pointed at the wrong database."""
        postgres.listing = toc("wordpress_posts")

        with pytest.raises(backups.BackupError, match="not a backup of this application"):
            backups.create()

    def test_pg_restore_closing_the_pipe_early_is_not_a_failure(self, backup_root, postgres):
        """It reads the table of contents and stops, which leaves us writing into a
        closed pipe. The frames were all authenticated by the pass before this one,
        so there is nothing left to prove."""
        postgres.restore_stdin_fails_after = 100
        postgres.dump = archive(200_000)

        assert backups.create().verified

    def test_a_backup_that_fails_verification_is_renamed_out_of_the_way(
        self, backup_root, postgres
    ):
        """Kept for diagnosis, but no longer named like something to restore from."""
        postgres.listing = toc("wordpress_posts")

        with pytest.raises(backups.BackupError):
            backups.create()

        assert backups.existing_backups(backup_root) == []
        quarantined = list(backup_root.glob("*.unverified"))
        assert len(quarantined) == 1
        assert Path(str(quarantined[0]) + ".json").exists()

    def test_verification_can_be_skipped_but_is_reported_as_skipped(self, backup_root, postgres):
        result = backups.create(verify_after=False)

        assert result.verified is False
        assert postgres.restore_calls == []


# --- pruning --------------------------------------------------------------


class TestPruning:
    def test_backups_past_the_window_are_deleted_with_their_manifests(
        self, backup_root, postgres, settings
    ):
        settings.BACKUP_KEEP_DAYS = 14
        old = aged_backup(backup_root, days=30)

        result = backups.create()

        assert not old.exists()
        assert not backups.manifest_path(old).exists()
        assert old.name in result.pruned

    def test_a_backup_inside_the_window_is_kept(self, backup_root, postgres, settings):
        settings.BACKUP_KEEP_DAYS = 14
        recent = aged_backup(backup_root, days=3)

        backups.create()

        assert recent.exists()

    def test_the_newest_backup_is_never_deleted_however_old_it_is(self, backup_root, settings):
        """The failure being guarded against is a month of broken dumps quietly
        deleting the last one that worked."""
        settings.BACKUP_KEEP_DAYS = 14
        ancient = aged_backup(backup_root, days=400)

        removed = backups.prune(root=backup_root, keep_days=14)

        assert removed == []
        assert ancient.exists()

    def test_nothing_is_pruned_when_the_new_backup_does_not_verify(
        self, backup_root, postgres, settings
    ):
        """Ordering, and the reason it is worth a test: deleting last night's good
        backup because tonight's ran is exactly how a backup directory ends up
        holding nothing usable."""
        settings.BACKUP_KEEP_DAYS = 1
        old = aged_backup(backup_root, days=30)
        older = aged_backup(backup_root, days=60)
        postgres.listing = toc("wordpress_posts")

        with pytest.raises(backups.BackupError):
            backups.create()

        assert old.exists()
        assert older.exists()

    def test_files_that_are_not_backups_are_left_alone(self, backup_root, postgres):
        """This directory belongs to an operator as much as to the application. A
        prune that deleted by glob would eventually delete somebody's notes, or the
        off-box copy's state file."""
        backup_root.mkdir(parents=True, exist_ok=True)
        notes = backup_root / "how-to-restore.txt"
        notes.write_text("read docs/restore-drill.md")
        misnamed = backup_root / "bctracker-last-week.dump.enc"
        misnamed.write_bytes(b"?")

        backups.prune(root=backup_root, keep_days=0)

        assert notes.exists()
        assert misnamed.exists()

    def test_an_abandoned_partial_file_is_cleaned_up(self, backup_root, postgres):
        """Left by a run that was killed. Harmless, but it accumulates and it is the
        one thing in this directory that looks like a backup and is not."""
        backup_root.mkdir(parents=True, exist_ok=True)
        stale = backup_root / "bctracker-20260101T000000Z.dump.enc.part"
        stale.write_bytes(b"half a dump")
        old = dt.datetime.now().timestamp() - dt.timedelta(days=3).total_seconds()
        os.utime(stale, (old, old))
        fresh = backup_root / "bctracker-20260909T000000Z.dump.enc.part"
        fresh.write_bytes(b"a dump being written right now")

        backups.prune(root=backup_root, keep_days=14)

        assert not stale.exists()
        assert fresh.exists()


# --- the commands ---------------------------------------------------------


class TestTheBackupCommand:
    def test_it_writes_a_backup_and_says_where(self, backup_root, postgres, capsys):
        call_command("backup_database")

        output = capsys.readouterr().out
        assert len(backups.existing_backups(backup_root)) == 1
        assert "verified" in output
        assert "off-box" in output

    def test_it_records_the_backup_in_the_audit_trail(self, backup_root, postgres):
        call_command("backup_database")

        event = AuditEvent.objects.filter(verb=AuditVerb.BACKUP_CREATED).get()
        assert event.actor is None  # the cron sidecar, not a person
        assert event.metadata["verified"] is True
        assert event.metadata["filename"].endswith(".dump.enc")

    def test_a_failure_is_recorded_and_is_a_non_zero_exit(self, backup_root, postgres):
        """The exit status is what a monitoring check reads, and the audit row is
        what an administrator sees a week later — the container log has scrolled
        away by then."""
        postgres.dump_status = 1
        postgres.dump_stderr = b"pg_dump: error: out of disk"

        with pytest.raises(CommandError):
            call_command("backup_database")

        event = AuditEvent.objects.filter(verb=AuditVerb.BACKUP_FAILED).get()
        assert "out of disk" in event.metadata["error"]

    def test_it_can_be_pointed_somewhere_else(self, backup_root, postgres, tmp_path):
        elsewhere = tmp_path / "elsewhere"

        call_command("backup_database", "--output-dir", str(elsewhere))

        assert len(backups.existing_backups(elsewhere)) == 1
        assert not backup_root.exists()

    def test_skipping_verification_is_visible_in_the_output(self, backup_root, postgres, capsys):
        call_command("backup_database", "--no-verify")

        assert "not verified" in capsys.readouterr().out


class TestTheDecryptCommand:
    def test_it_lists_what_is_available(self, backup_root, postgres, capsys):
        result = backups.create()

        call_command("decrypt_backup", "--list")

        assert result.dump.name in capsys.readouterr().out

    def test_with_no_arguments_it_verifies_the_newest(self, backup_root, postgres, capsys):
        backups.create()

        call_command("decrypt_backup")

        output = capsys.readouterr().out
        assert "Verified" in output
        assert backups.key_fingerprint() in output

    def test_it_writes_the_plaintext_dump_where_it_is_told(
        self, backup_root, postgres, tmp_path, capsys
    ):
        backups.create()
        destination = tmp_path / "drill.dump"

        call_command("decrypt_backup", "--output", str(destination))

        assert destination.read_bytes() == postgres.dump
        assert "pg_restore" in capsys.readouterr().out

    def test_decrypting_a_backup_is_audited(self, backup_root, postgres, tmp_path):
        """A plaintext dump is every counseling record this ministry holds, in one
        file, produced at a shell rather than through a view. It is recorded for the
        same reason a document download is."""
        backups.create()

        call_command("decrypt_backup", "--output", str(tmp_path / "drill.dump"))

        event = AuditEvent.objects.filter(verb=AuditVerb.BACKUP_DECRYPTED).get()
        assert event.metadata["plaintext_bytes"] == len(postgres.dump)

    def test_it_refuses_to_overwrite_an_existing_file(self, backup_root, postgres, tmp_path):
        backups.create()
        destination = tmp_path / "drill.dump"
        destination.write_bytes(b"something else")

        with pytest.raises(CommandError, match="refusing to overwrite"):
            call_command("decrypt_backup", "--output", str(destination))

        assert destination.read_bytes() == b"something else"

    @pytest.mark.parametrize("directory", ["backups", "documents"])
    def test_it_refuses_to_write_plaintext_where_ciphertext_is_replicated(
        self, backup_root, postgres, settings, tmp_path, directory
    ):
        """Both directories are copied off this host. A plaintext dump written into
        either would be replicated as-is, which is the whole encryption scheme
        undone by one convenient path."""
        backups.create()
        target = {
            "backups": backup_root,
            "documents": Path(settings.DOCUMENT_STORE_ROOT),
        }[directory]

        with pytest.raises(CommandError, match="Refusing to write a plaintext dump"):
            call_command("decrypt_backup", "--output", str(target / "drill.dump"))

    def test_a_partial_decryption_leaves_no_file(self, backup_root, postgres, tmp_path):
        """A plaintext dump that stops halfway is useless and still sensitive."""
        postgres.dump = archive(200_000)
        result = backups.create()
        stored = bytearray(result.dump.read_bytes())
        stored[-20] ^= 0x01  # inside the last frame, after the first has been written
        result.dump.write_bytes(stored)
        destination = tmp_path / "drill.dump"

        with pytest.raises(CommandError, match="could not be decrypted"):
            call_command("decrypt_backup", "--output", str(destination))

        assert not destination.exists()

    def test_a_backup_that_fails_verification_is_recorded(self, backup_root, postgres):
        """This command is what the weekly cron tick runs to catch a file that has
        decayed since it was written, so the failure has to reach somewhere an
        administrator looks."""
        result = backups.create()
        stored = bytearray(result.dump.read_bytes())
        stored[-1] ^= 0x01
        result.dump.write_bytes(stored)

        with pytest.raises(CommandError):
            call_command("decrypt_backup")

        assert AuditEvent.objects.filter(verb=AuditVerb.BACKUP_FAILED).exists()

    def test_it_says_so_when_there_is_nothing_to_read(self, backup_root, postgres):
        with pytest.raises(CommandError, match="No backups found"):
            call_command("decrypt_backup")


# --- deploy checks --------------------------------------------------------


class TestTheDeployChecks:
    """A backup that cannot run is invisible until it is needed, which is the worst
    property a control can have. These make it a boot failure instead."""

    def _run(self):
        from django.core.checks import run_checks

        return {problem.id for problem in run_checks(include_deployment_checks=True)}

    def test_a_backup_directory_the_web_server_publishes_is_reported(self, settings):
        """It would be ciphertext, but there is no reason to serve the whole
        database at a guessable URL along with the manifest naming its key."""
        settings.BACKUP_ROOT = Path(settings.STATIC_ROOT) / "backups"

        assert "backups.E001" in self._run()

    def test_a_backup_directory_outside_it_is_fine(self, settings, tmp_path):
        settings.BACKUP_ROOT = tmp_path / "backups"

        assert "backups.E001" not in self._run()

    def test_missing_postgres_client_binaries_are_reported(self, settings):
        """Both, separately: a container that can write backups but not read them
        back would pass every nightly run and fail the one that mattered."""
        settings.PG_DUMP_PATH = "pg_dump-that-is-not-installed"
        settings.PG_RESTORE_PATH = "pg_restore-that-is-not-installed"

        reported = self._run()
        assert "backups.E002" in reported
        assert "backups.E003" in reported
