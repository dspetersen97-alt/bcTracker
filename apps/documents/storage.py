"""
Where the ciphertext lives.

Two rules this module exists to keep:

  * **On-disk names carry no information.** A file is named by its opaque
    ``storage_key`` UUID, never by the original filename. The real name lives in
    the database, so a directory listing — or a leaked backup index — says
    nothing about whose file it is or what it contains.
  * **Nothing here is reachable by URL.** There is no MEDIA_ROOT and no static
    mapping onto this directory; the only way out is a permission-checked view
    (apps/documents/views.py).

Paths are sharded two levels deep on the first four hex characters of the UUID,
so no single directory accumulates every document the ministry has ever stored.
"""

import os
from pathlib import Path
from uuid import UUID

from django.conf import settings


def store_root() -> Path:
    return Path(settings.DOCUMENT_STORE_ROOT)


def blob_path(storage_key: UUID | str) -> Path:
    key = str(storage_key)
    return store_root() / key[:2] / key[2:4] / key


def ensure_parent(path: Path) -> None:
    # 0o700: the container's app user is the only account that should be able to
    # list this tree. The dataset it sits on is encrypted, but permissions are
    # what stop another process on the same host from reading it.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def write_blob(storage_key: UUID | str, writer) -> Path:
    """Create the file for ``storage_key`` and hand the handle to ``writer``.

    ``writer(handle)`` does the encrypting. Written to a ``.part`` file and then
    renamed, so a crash mid-upload cannot leave a truncated blob that looks
    complete — the rename is atomic on the same filesystem.
    """
    path = blob_path(storage_key)
    ensure_parent(path)
    staging = path.with_suffix(".part")
    try:
        with open(staging, "wb") as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    path.chmod(0o600)
    return path


def open_blob(storage_key: UUID | str):
    return open(blob_path(storage_key), "rb")


def blob_exists(storage_key: UUID | str) -> bool:
    return blob_path(storage_key).exists()


def delete_blob(storage_key: UUID | str) -> bool:
    """Remove the ciphertext. Returns whether there was anything to remove.

    Only called by a real purge, never by the soft delete a user performs: a
    document a counselee "deleted" must still be recoverable, and its audit rows
    must still refer to something that exists.
    """
    path = blob_path(storage_key)
    if not path.exists():
        return False
    path.unlink()
    return True
