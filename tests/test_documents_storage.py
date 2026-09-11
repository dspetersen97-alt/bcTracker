"""
The volume the ciphertext sits on.

Two properties are asserted here that no amount of correct cryptography would
give us on its own:

  * a directory listing of the store says nothing — no names, no case, no
    counselee, nothing but UUIDs;
  * a write either produces a complete blob or produces nothing. A half-written
    file that looks finished is the failure mode a restore or a crash would
    otherwise hand us, and it is indistinguishable from tampering.

``tests/conftest.py`` points ``DOCUMENT_STORE_ROOT`` at a temporary directory for
every test in the suite, so nothing here can touch a real store.
"""

import sys
from uuid import uuid4

import pytest

from apps.documents import storage

pytestmark = pytest.mark.filterwarnings("error")


class TestWhereFilesLand:
    def test_a_blob_is_named_only_by_its_uuid(self, _document_store):
        key = uuid4()

        storage.write_blob(key, lambda handle: handle.write(b"sealed"))

        names = [path.name for path in _document_store.rglob("*") if path.is_file()]
        assert names == [str(key)]

    def test_the_original_filename_is_nowhere_on_disk(self, _document_store):
        """The name is metadata, and metadata lives in the database.

        "Ashford-restraining-order.pdf" in a directory listing would disclose the
        matter to anyone who could list the volume, ciphertext or not.
        """
        key = uuid4()

        storage.write_blob(key, lambda handle: handle.write(b"sealed"))

        listing = " ".join(str(path) for path in _document_store.rglob("*"))
        assert "Ashford" not in listing
        assert ".pdf" not in listing

    def test_files_are_sharded_so_no_directory_grows_without_bound(self):
        key = uuid4()

        path = storage.blob_path(key)

        assert path.parent.name == str(key)[2:4]
        assert path.parent.parent.name == str(key)[:2]

    def test_what_was_written_is_what_is_read_back(self):
        key = uuid4()
        storage.write_blob(key, lambda handle: handle.write(b"sealed bytes"))

        with storage.open_blob(key) as handle:
            assert handle.read() == b"sealed bytes"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file modes are not meaningful on Windows; the container is Linux",
    )
    def test_only_the_application_user_can_read_a_blob(self):
        """0o600, because the encrypted dataset protects against a stolen drive.

        It does nothing about another process on a running host, and permissions
        are the only thing that does.
        """
        key = uuid4()

        path = storage.write_blob(key, lambda handle: handle.write(b"sealed"))

        assert path.stat().st_mode & 0o777 == 0o600


class TestAFailedWrite:
    def test_leaves_no_blob_behind(self, _document_store):
        key = uuid4()

        def fail(handle):
            handle.write(b"the first half")
            raise RuntimeError("the scanner died mid-write")

        with pytest.raises(RuntimeError):
            storage.write_blob(key, fail)

        assert not storage.blob_exists(key)

    def test_leaves_no_staging_file_behind_either(self, _document_store):
        """A .part left lying around is a slow leak of counselee data.

        It would also be invisible: nothing in the database refers to it, so no
        retention job would ever look for it.
        """
        key = uuid4()

        with pytest.raises(RuntimeError):
            storage.write_blob(key, lambda handle: (_ for _ in ()).throw(RuntimeError("no")))

        assert not list(_document_store.rglob("*.part"))

    def test_an_interrupted_write_is_cleaned_up_too(self, _document_store):
        """BaseException, not Exception: a worker timeout must not leave a stub.

        gunicorn kills a hung worker, and that arrives as something that does not
        descend from Exception.
        """
        key = uuid4()

        def interrupted(handle):
            handle.write(b"partial")
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            storage.write_blob(key, interrupted)

        assert not storage.blob_exists(key)
        assert not list(_document_store.rglob("*.part"))


class TestRemoval:
    def test_deleting_reports_whether_there_was_anything_to_delete(self):
        key = uuid4()
        storage.write_blob(key, lambda handle: handle.write(b"sealed"))

        assert storage.delete_blob(key) is True
        assert storage.delete_blob(key) is False

    def test_opening_a_blob_that_was_never_written_raises(self):
        """What a restore that missed the document volume looks like.

        The view turns this into a 404 and logs it loudly, because a database
        restored without its documents is a situation someone needs to be told
        about rather than shown as a missing page.
        """
        with pytest.raises(FileNotFoundError):
            storage.open_blob(uuid4())
