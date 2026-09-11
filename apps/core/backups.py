"""
Encrypted database backups: dump, encrypt, verify, prune.

The nightly job (compose/cron/bctracker.cron) streams ``pg_dump`` straight into
the same envelope encryption the documents use, so no plaintext copy of the
database ever exists on disk. The database is the half of this system that holds
counselee names, case labels, appointment times, and counselor notes; the
documents volume is already ciphertext file-by-file, and a dump is not.

Four decisions worth knowing:

  * **Encrypted with the master key.** This raises the stakes on
    ``BCTRACKER_MASTER_KEY`` — lose it and the backups become unreadable too, not
    only the documents. That is the deliberate trade: a second key would mean a
    second custody problem, and an unencrypted dump pushed off-box is the most
    likely way this ministry's records end up somewhere it did not choose. Each
    backup records a *fingerprint* of the key that sealed it (see
    ``key_fingerprint``) so a restore does not begin by guessing.

  * **Verified, not merely written.** ``create()`` reads the file back, which
    authenticates every frame with the key just used, and pipes the plaintext
    through ``pg_restore --list`` to confirm the archive is real and lists this
    application's tables. An unverified backup is a hope. A backup that fails
    verification is renamed out of the way rather than left looking usable.

  * **Nothing is pruned until the new backup verifies**, and the newest backup is
    never pruned however old it is. The failure mode being avoided is a month of
    broken dumps quietly deleting the last good one.

  * **The password is passed in the environment, never in argv.** Anything on the
    host can read another process's command line.

Getting the backup *off this host* is a separate, mandatory step; see
docs/restore-drill.md. A backup on the same pool as the database survives a
mistake, not a fire.
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from django.conf import settings
from django.utils import timezone

from apps.documents import crypto

#: Written into every manifest. A reader that does not recognise it should stop
#: rather than guess at the layout.
MANIFEST_FORMAT = "bctracker-backup/1"

#: UTC, second resolution, sortable as text — the file listing is in chronological
#: order without parsing anything.
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

DUMP_SUFFIX = ".dump.enc"
MANIFEST_SUFFIX = ".json"
PARTIAL_SUFFIX = ".part"
#: What a backup that failed verification is renamed to. Kept for diagnosis, but
#: no longer named like something a restore should reach for.
QUARANTINE_SUFFIX = ".unverified"

NAME_PATTERN = re.compile(r"^bctracker-\d{8}T\d{6}Z" + re.escape(DUMP_SUFFIX) + r"$")

#: Stale ``.part`` files older than this are left behind by a crashed or killed
#: run and are removed by the next prune.
PARTIAL_MAX_AGE = dt.timedelta(days=1)


class BackupError(Exception):
    """A backup could not be produced, or could not be read back.

    One exception type on purpose: to the operator these are one event — there is
    no usable backup from this run — and the message carries the detail.
    """


@dataclass(frozen=True)
class BackupResult:
    dump: Path
    manifest: Path
    plaintext_bytes: int
    ciphertext_bytes: int
    verified: bool = False
    listed_tables: int = 0
    pruned: list[str] = field(default_factory=list)


# --- locations ------------------------------------------------------------


def backup_root() -> Path:
    return Path(settings.BACKUP_ROOT)


def ensure_root(root: Path) -> Path:
    # 0o700 for the same reason the document store uses it: the encrypted dataset
    # protects a stolen drive, and permissions are what stop another process on a
    # running host from reading the whole database.
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def dump_path(root: Path, *, now) -> Path:
    stamp = now.astimezone(dt.UTC).strftime(STAMP_FORMAT)
    return root / f"bctracker-{stamp}{DUMP_SUFFIX}"


def manifest_path(dump: Path) -> Path:
    """The sidecar for a dump.

    Appended rather than substituted, so the pair sorts together in a listing and
    it is obvious which manifest belongs to which archive.
    """
    return Path(str(dump) + MANIFEST_SUFFIX)


def existing_backups(root: Path | None = None) -> list[Path]:
    """Every complete backup in the directory, oldest first.

    Matched against ``NAME_PATTERN`` rather than a glob, so a file an operator
    dropped in this directory is never mistaken for a backup — and, more
    importantly, never pruned as one.
    """
    root = root or backup_root()
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if NAME_PATTERN.match(path.name))


def latest_backup(root: Path | None = None) -> Path | None:
    backups = existing_backups(root)
    return backups[-1] if backups else None


# --- the child processes --------------------------------------------------


def spawn(argv, *, env=None, stdin=None, stdout=None, stderr=None):
    """Start ``pg_dump`` or ``pg_restore``.

    The one seam the tests replace. ``postgresql-client`` is installed in the
    application image (see the Dockerfile), but a developer's machine talks to
    Postgres in a container and has neither binary, so a test suite that shelled
    out for real would pass or fail depending on whose laptop it ran on. What the
    tests assert instead is the part that is actually easy to get wrong: the
    argument list, the environment, and what happens when the child fails.
    """
    # S603: the argument list is built by this module from settings, never from a
    # request, and it is a list rather than a shell string — there is no shell.
    return subprocess.Popen(  # noqa: S603
        argv, env=env, stdin=stdin, stdout=stdout, stderr=stderr
    )


def dump_argv(alias: str = "default") -> list[str]:
    database = settings.DATABASES[alias]
    argv = [
        settings.PG_DUMP_PATH,
        # The custom format is compressed and is what pg_restore needs for
        # selective restores. Compressing here also means the ciphertext is not
        # noticeably larger than the data — compressing *after* encryption would
        # do nothing at all.
        "--format=custom",
        # A restore may well go into a scratch database owned by a different role
        # during a drill, and ownership and grants are recreated by migrations
        # anyway.
        "--no-owner",
        "--no-privileges",
        # Never prompt. Without this a misconfigured password turns a nightly
        # backup into a process sitting forever on a tty that does not exist.
        "--no-password",
    ]
    if database.get("HOST"):
        argv += ["--host", str(database["HOST"])]
    if database.get("PORT"):
        argv += ["--port", str(database["PORT"])]
    if database.get("USER"):
        argv += ["--username", str(database["USER"])]
    argv += ["--dbname", str(database["NAME"])]
    return argv


def subprocess_env(alias: str = "default") -> dict:
    """The child's environment, carrying the password.

    In the environment rather than in ``--password`` or a URL because argv is
    world-readable through /proc on the host.
    """
    env = os.environ.copy()
    password = settings.DATABASES[alias].get("PASSWORD") or ""
    if password:
        env["PGPASSWORD"] = str(password)
    else:
        env.pop("PGPASSWORD", None)
    return env


def _tail(handle, limit: int = 2000) -> str:
    """The end of a child's stderr, for an error message."""
    try:
        handle.seek(0)
        return handle.read().decode("utf-8", errors="replace").strip()[-limit:]
    except OSError:  # pragma: no cover - a temp file that cannot be re-read
        return ""


# --- keys and manifests ---------------------------------------------------


def key_fingerprint() -> str:
    """A short identifier for the master key, safe to store beside ciphertext.

    A keyed digest of a fixed label, so it identifies which key a backup needs
    without being derived from anything an attacker could invert. During a restore
    this answers "is this the key that sealed that file" before anything is
    overwritten.
    """
    return hmac.new(
        crypto.master_key(), b"bctracker-backup-key-fingerprint", hashlib.sha256
    ).hexdigest()[:16]


def _applied_migrations() -> dict:
    """The newest applied migration per app.

    Recorded because a dump restored under code with a different schema fails in
    confusing ways, and this is the cheapest way for a drill to notice before
    starting.
    """
    from django.db.migrations.recorder import MigrationRecorder

    latest: dict[str, str] = {}
    rows = MigrationRecorder.Migration.objects.order_by("app", "name").values_list("app", "name")
    for app, name in rows:
        latest[app] = name
    return latest


def _document_store_summary() -> dict | None:
    """How many document blobs exist, and how many bytes they occupy.

    The documents volume is backed up by copying it, not by this command — the
    files are already individually encrypted, and duplicating them here would
    double the space used on the very host the backup needs to leave. What this
    records is a fingerprint, so a restore can tell whether the file store it has
    matches the database it is restoring rather than discovering the mismatch one
    missing download at a time.
    """
    root = Path(settings.DOCUMENT_STORE_ROOT)
    if not root.exists():
        return None
    files = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.name.endswith(PARTIAL_SUFFIX):
            files += 1
            total += path.stat().st_size
    return {"files": files, "bytes": total}


def write_manifest(dump: Path, payload: dict) -> Path:
    path = manifest_path(dump)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def read_manifest(dump: Path) -> dict:
    path = manifest_path(dump)
    if not path.exists():
        raise BackupError(
            f"{path.name} is missing. The manifest holds the wrapped key for "
            f"{dump.name}, which cannot be decrypted without it."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"{path.name} is not readable JSON: {exc}") from exc
    if payload.get("format") != MANIFEST_FORMAT:
        raise BackupError(
            f"{path.name} is in format {payload.get('format')!r}, not {MANIFEST_FORMAT!r}."
        )
    return payload


def unwrap(manifest: dict) -> tuple[bytes, UUID]:
    """The DEK for a backup, and the storage key its frames are bound to."""
    try:
        storage_key = UUID(manifest["storage_key"])
        wrapped = base64.b64decode(manifest["wrapped_dek"], validate=True)
        nonce = base64.b64decode(manifest["dek_nonce"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise BackupError(f"The manifest is missing or has a malformed key: {exc}") from exc

    fingerprint = manifest.get("master_key_fingerprint")
    if fingerprint and fingerprint != key_fingerprint():
        raise BackupError(
            "This backup was sealed with a different BCTRACKER_MASTER_KEY "
            f"(manifest says {fingerprint}, this process has {key_fingerprint()})."
        )
    # BackupError is the boundary of this module: a caller decides what to do
    # about a backup it cannot read, and should not have to know that the reason
    # arrived from the crypto layer.
    try:
        return crypto.unwrap_dek(wrapped, nonce, storage_key=storage_key), storage_key
    except crypto.DecryptionError as exc:
        raise BackupError(f"The stored key could not be unwrapped: {exc}") from exc


# --- reading a backup back -----------------------------------------------


class _Digesting:
    """Wraps a file object, hashing and counting what passes through it.

    Used on both directions so the ciphertext digest is computed in the same pass
    that writes or authenticates it, rather than in an extra read of a file that
    may be gigabytes.
    """

    def __init__(self, handle):
        self._handle = handle
        self.digest = hashlib.sha256()
        self.bytes = 0

    def write(self, data):
        self.digest.update(data)
        self.bytes += len(data)
        return self._handle.write(data)

    def read(self, size=-1):
        data = self._handle.read(size)
        self.digest.update(data)
        self.bytes += len(data)
        return data

    @property
    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def plaintext_frames(dump: Path, manifest: dict | None = None):
    """Yield the decrypted dump, frame by frame.

    Every frame is authenticated as it is read, so a caller that consumes the
    whole generator has proved the file is byte-for-byte what was written under
    the key this process holds. A caller that stops early has proved only that
    the beginning is.
    """
    manifest = manifest if manifest is not None else read_manifest(dump)
    dek, storage_key = unwrap(manifest)
    try:
        with open(dump, "rb") as handle:
            yield from crypto.decrypt_stream(handle, dek=dek, storage_key=storage_key)
    except crypto.DecryptionError as exc:
        raise BackupError(f"{dump.name} could not be decrypted: {exc}") from exc


def verify(dump: Path, *, alias: str = "default") -> dict:
    """Read a backup back, two ways, and raise if either says no.

    First the whole file is decrypted and digested: that authenticates every
    frame under the current master key and detects truncation, corruption, and a
    file that has been swapped for another. Then the plaintext is piped through
    ``pg_restore --list``, which proves the bytes are a real archive whose table
    of contents mentions this application's tables — the check that catches a
    ``pg_dump`` that exited zero having written nothing useful.
    """
    manifest = read_manifest(dump)
    dek, storage_key = unwrap(manifest)

    plaintext_bytes = 0
    try:
        with open(dump, "rb") as handle:
            reader = _Digesting(handle)
            for frame in crypto.decrypt_stream(reader, dek=dek, storage_key=storage_key):
                plaintext_bytes += len(frame)
            digest = reader.hexdigest
    except crypto.DecryptionError as exc:
        raise BackupError(f"{dump.name} could not be decrypted: {exc}") from exc

    expected = manifest.get("sha256")
    if expected and expected != digest:
        raise BackupError(f"{dump.name} does not match the digest recorded when it was written.")
    if manifest.get("plaintext_bytes") not in (None, plaintext_bytes):
        raise BackupError(
            f"{dump.name} decrypts to {plaintext_bytes} bytes, but the manifest "
            f"records {manifest['plaintext_bytes']}."
        )

    listing = _list_archive(dump, manifest, alias=alias)
    return {
        "plaintext_bytes": plaintext_bytes,
        "sha256": digest,
        "listed_tables": listing.count("TABLE DATA"),
    }


def _list_archive(dump: Path, manifest: dict, *, alias: str = "default") -> str:
    """Pipe the decrypted dump through ``pg_restore --list`` and return its output.

    stdout goes to a temporary file rather than a pipe we read while also writing
    to stdin: two pipes and one thread is how a job like this deadlocks on a
    table of contents that happens to be larger than a pipe buffer.
    """
    from django.contrib.auth import get_user_model

    argv = [settings.PG_RESTORE_PATH, "--list"]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as errors:
        process = spawn(
            argv,
            env=subprocess_env(alias),
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=errors,
        )
        try:
            for frame in plaintext_frames(dump, manifest):
                process.stdin.write(frame)
        except (BrokenPipeError, OSError):
            # pg_restore stops reading once it has the table of contents, which is
            # near the start of a custom-format archive. Not a failure: the frames
            # were all authenticated by the pass above this one.
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        status = process.wait()
        out.seek(0)
        listing = out.read().decode("utf-8", errors="replace")
        if status != 0:
            raise BackupError(
                f"pg_restore could not read {dump.name} (exit {status}): {_tail(errors)}"
            )

    table = get_user_model()._meta.db_table
    if table not in listing:
        raise BackupError(
            f"{dump.name} is a readable archive but its contents list no {table} "
            "table, so it is not a backup of this application."
        )
    return listing


# --- making one ----------------------------------------------------------


def create(
    *,
    root: Path | None = None,
    alias: str = "default",
    verify_after: bool = True,
    keep_days: int | None = None,
    now=None,
) -> BackupResult:
    """Dump the database, encrypted, and return where it landed.

    Raises ``BackupError`` for anything that leaves no usable backup. Nothing is
    pruned unless this one verified.
    """
    root = ensure_root(root or backup_root())
    now = now or timezone.now()
    keep_days = settings.BACKUP_KEEP_DAYS if keep_days is None else keep_days

    dump = dump_path(root, now=now)
    if dump.exists():
        # Two runs in the same second, which in practice means a cron tick
        # overlapping a manual run. Refusing is right: the alternative is one of
        # them silently overwriting a file the other is still verifying.
        raise BackupError(f"{dump.name} already exists.")

    storage_key = uuid4()
    dek = crypto.generate_dek()
    # Raises ImproperlyConfigured if BCTRACKER_MASTER_KEY is unset, before
    # pg_dump is started rather than after the database has been read out.
    wrapped_dek, dek_nonce = crypto.wrap_dek(dek, storage_key=storage_key)
    fingerprint = key_fingerprint()

    staging = Path(str(dump) + PARTIAL_SUFFIX)
    argv = dump_argv(alias)
    try:
        with tempfile.TemporaryFile() as errors:
            with open(staging, "wb") as handle:
                writer = _Digesting(handle)
                process = spawn(
                    argv,
                    env=subprocess_env(alias),
                    stdout=subprocess.PIPE,
                    stderr=errors,
                )
                plaintext_bytes = crypto.encrypt_stream(
                    process.stdout, writer, dek=dek, storage_key=storage_key
                )
                process.stdout.close()
                status = process.wait()
                handle.flush()
                os.fsync(handle.fileno())
            if status != 0:
                raise BackupError(f"pg_dump exited with status {status}: {_tail(errors)}")
        if plaintext_bytes == 0:
            raise BackupError("pg_dump succeeded but produced no output.")
        os.replace(staging, dump)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    dump.chmod(0o600)

    manifest = write_manifest(
        dump,
        {
            "format": MANIFEST_FORMAT,
            "created_at": now.isoformat(),
            "database": settings.DATABASES[alias]["NAME"],
            "storage_key": str(storage_key),
            "wrapped_dek": base64.b64encode(wrapped_dek).decode(),
            "dek_nonce": base64.b64encode(dek_nonce).decode(),
            "master_key_fingerprint": fingerprint,
            "plaintext_bytes": plaintext_bytes,
            "ciphertext_bytes": writer.bytes,
            "sha256": writer.hexdigest,
            "migrations": _applied_migrations(),
            "documents": _document_store_summary(),
        },
    )

    result = BackupResult(
        dump=dump,
        manifest=manifest,
        plaintext_bytes=plaintext_bytes,
        ciphertext_bytes=writer.bytes,
    )
    if not verify_after:
        return result

    try:
        checked = verify(dump, alias=alias)
    except BackupError:
        quarantine(dump)
        raise

    pruned = prune(root=root, keep_days=keep_days, now=now)
    return BackupResult(
        dump=dump,
        manifest=manifest,
        plaintext_bytes=plaintext_bytes,
        ciphertext_bytes=writer.bytes,
        verified=True,
        listed_tables=checked["listed_tables"],
        pruned=[path.name for path in pruned],
    )


def quarantine(dump: Path) -> Path:
    """Rename a backup that failed verification, with its manifest.

    So that a file which cannot be restored is not sitting in the backup
    directory looking exactly like one that can. It is kept rather than deleted:
    whatever went wrong is easier to diagnose with the bytes in hand.
    """
    moved = Path(str(dump) + QUARANTINE_SUFFIX)
    os.replace(dump, moved)
    source_manifest = manifest_path(dump)
    if source_manifest.exists():
        os.replace(source_manifest, Path(str(moved) + MANIFEST_SUFFIX))
    return moved


def prune(*, root: Path, keep_days: int, now=None, keep_at_least: int = 1) -> list[Path]:
    """Delete backups older than the window, and abandoned ``.part`` files.

    ``keep_at_least`` is what stops a month of failing dumps from ending with no
    backup at all: however old the newest one is, it stays. Only files whose names
    this module generated are considered, so anything else in the directory is
    left alone.
    """
    now = now or timezone.now()
    cutoff = now - dt.timedelta(days=keep_days)
    backups = existing_backups(root)
    removed = []

    candidates = backups[:-keep_at_least] if keep_at_least else backups
    for path in candidates:
        stamp = path.name[len("bctracker-") : -len(DUMP_SUFFIX)]
        created = dt.datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=dt.UTC)
        if created >= cutoff:
            continue
        manifest_path(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        removed.append(path)

    for path in root.glob(f"*{PARTIAL_SUFFIX}"):
        age = now.timestamp() - path.stat().st_mtime
        if age > PARTIAL_MAX_AGE.total_seconds():
            path.unlink(missing_ok=True)
            removed.append(path)

    return removed
