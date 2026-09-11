"""
The envelope scheme, attacked rather than demonstrated.

A round-trip test proves almost nothing: XOR with a constant would pass one. What
is worth asserting is that each of the specific things the framing exists to stop
actually fails — a frame moved, a frame borrowed from another document, bytes
removed from the end, bytes added to the end — because those are the attacks
available to whoever has the volume but not the master key. Every one of them is
a plausible outcome of a partial restore too, which is the other reason to care:
a document that silently decrypts to the wrong bytes is worse than one that
refuses.

Nothing here goes near the database. ``storage_key`` is any UUID, so these tests
are about the format alone and stay fast enough to run on every commit.
"""

import io
import os
import struct
from uuid import uuid4

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.documents import crypto

pytestmark = pytest.mark.filterwarnings("error")


def frames_of(blob: bytes) -> tuple[bytes, list[bytes]]:
    """Split a stored blob into its header and its sealed frames.

    Tests that tamper with the format need to take it apart, and doing that here
    once means each test reads as the attack it is rather than as struct
    arithmetic.
    """
    header, rest = blob[: crypto.HEADER_SIZE], blob[crypto.HEADER_SIZE :]
    frames = []
    while rest:
        (length,) = struct.unpack(">I", rest[: crypto.LENGTH_SIZE])
        start = crypto.LENGTH_SIZE
        frames.append(rest[start : start + length])
        rest = rest[start + length :]
    return header, frames


def reassemble(header: bytes, frames: list[bytes]) -> bytes:
    return header + b"".join(struct.pack(">I", len(frame)) + frame for frame in frames)


@pytest.fixture
def sealed():
    """A three-frame document, with the key and id needed to open it."""
    key = uuid4()
    dek = crypto.generate_dek()
    plaintext = os.urandom(crypto.FRAME_SIZE * 2 + 1000)
    blob = crypto.encrypt_bytes(plaintext, dek=dek, storage_key=key)
    return {"key": key, "dek": dek, "plaintext": plaintext, "blob": blob}


class TestRoundTrip:
    @pytest.mark.parametrize(
        "size",
        [
            0,
            1,
            crypto.FRAME_SIZE - 1,
            # Exactly one frame: the read-ahead has to notice the stream ended and
            # still flag that frame final, or a whole class of file would look
            # truncated to its own reader.
            crypto.FRAME_SIZE,
            crypto.FRAME_SIZE + 1,
            crypto.FRAME_SIZE * 3,
        ],
    )
    def test_what_goes_in_comes_out(self, size):
        key, dek = uuid4(), crypto.generate_dek()
        plaintext = os.urandom(size)

        blob = crypto.encrypt_bytes(plaintext, dek=dek, storage_key=key)

        assert crypto.decrypt_bytes(blob, dek=dek, storage_key=key) == plaintext

    def test_the_plaintext_is_not_in_the_ciphertext(self):
        """The obvious sanity check, worth having explicitly.

        A "store it encrypted" bug that leaves the body in the clear would pass
        every tamper test in this file, because a reader that ignores the
        ciphertext still round-trips.
        """
        key, dek = uuid4(), crypto.generate_dek()
        secret = b"Ada disclosed the affair in session three." * 40

        blob = crypto.encrypt_bytes(secret, dek=dek, storage_key=key)

        assert secret not in blob
        assert b"affair" not in blob

    def test_the_byte_count_is_the_plaintext_length(self):
        """encrypt_stream's return value is what Document.byte_size records."""
        key, dek = uuid4(), crypto.generate_dek()
        source = io.BytesIO(os.urandom(crypto.FRAME_SIZE + 7))

        written = crypto.encrypt_stream(source, io.BytesIO(), dek=dek, storage_key=key)

        assert written == crypto.FRAME_SIZE + 7

    def test_decrypting_yields_frames_rather_than_one_string(self):
        """Streaming is the point: a 25 MB download must not become 25 MB of RAM."""
        key, dek = uuid4(), crypto.generate_dek()
        blob = crypto.encrypt_bytes(b"x" * (crypto.FRAME_SIZE * 2), dek=dek, storage_key=key)

        produced = list(crypto.decrypt_stream(io.BytesIO(blob), dek=dek, storage_key=key))

        assert len(produced) == 2
        assert all(len(frame) == crypto.FRAME_SIZE for frame in produced)

    def test_two_identical_documents_do_not_produce_identical_blobs(self):
        """Same bytes, same DEK, different nonce prefix — so no equality oracle.

        Without this an observer with the volume could tell that two counselees
        had uploaded the same worksheet.
        """
        key, dek = uuid4(), crypto.generate_dek()
        payload = b"the same handout, twice"

        first = crypto.encrypt_bytes(payload, dek=dek, storage_key=key)
        second = crypto.encrypt_bytes(payload, dek=dek, storage_key=key)

        assert first != second


class TestTheKey:
    def test_the_wrong_dek_does_not_open_it(self, sealed):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(
                sealed["blob"], dek=crypto.generate_dek(), storage_key=sealed["key"]
            )

    def test_a_dek_is_wrapped_and_unwrapped(self):
        key = uuid4()
        dek = crypto.generate_dek()

        wrapped, nonce = crypto.wrap_dek(dek, storage_key=key)

        assert wrapped != dek
        assert crypto.unwrap_dek(wrapped, nonce, storage_key=key) == dek

    def test_a_wrapped_dek_moved_to_another_row_does_not_unwrap(self):
        """The storage key is the wrap's AAD, so swapping two rows' keys fails.

        This is the database-side version of cross-file substitution: someone with
        UPDATE on the documents table, but no master key, cannot point one row's
        key material at another row's blob.
        """
        dek = crypto.generate_dek()
        wrapped, nonce = crypto.wrap_dek(dek, storage_key=uuid4())

        with pytest.raises(crypto.DecryptionError):
            crypto.unwrap_dek(wrapped, nonce, storage_key=uuid4())

    def test_a_tampered_wrapped_dek_does_not_unwrap(self):
        key = uuid4()
        wrapped, nonce = crypto.wrap_dek(crypto.generate_dek(), storage_key=key)
        flipped = bytearray(wrapped)
        flipped[0] ^= 0x01

        with pytest.raises(crypto.DecryptionError):
            crypto.unwrap_dek(bytes(flipped), nonce, storage_key=key)

    def test_changing_the_master_key_makes_wrapped_deks_unreadable(self, settings):
        """The risk register's first entry, as a test.

        Rotating the master key without rewrapping is indistinguishable from
        losing it. Anyone tempted to "just generate a new one" should be able to
        read this and see what it costs.
        """
        import base64

        key = uuid4()
        wrapped, nonce = crypto.wrap_dek(crypto.generate_dek(), storage_key=key)

        settings.DOCUMENT_MASTER_KEY = base64.b64encode(os.urandom(32)).decode()

        with pytest.raises(crypto.DecryptionError):
            crypto.unwrap_dek(wrapped, nonce, storage_key=key)

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "not base64 at all",
            # Valid base64, wrong length. Silently accepting this would give a
            # 16-byte key where the design says 32.
            "c2hvcnQta2V5",
        ],
    )
    def test_an_unusable_master_key_is_a_configuration_error(self, settings, value):
        settings.DOCUMENT_MASTER_KEY = value

        with pytest.raises(ImproperlyConfigured):
            crypto.master_key()


class TestTamperDetection:
    def test_flipping_one_ciphertext_bit_is_detected(self, sealed):
        corrupted = bytearray(sealed["blob"])
        corrupted[crypto.HEADER_SIZE + crypto.LENGTH_SIZE + 20] ^= 0x01

        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(bytes(corrupted), dek=sealed["dek"], storage_key=sealed["key"])

    def test_a_frame_from_another_document_is_detected(self, sealed):
        """Cross-file substitution, with a *valid* key for both files.

        The attacker here is not an outsider — it is a bug, or an administrator
        with disk access, moving a frame between two documents encrypted under the
        same DEK. Without the storage key in the AAD this would authenticate
        perfectly and hand one counselee another's page.
        """
        other_key = uuid4()
        other = crypto.encrypt_bytes(
            os.urandom(crypto.FRAME_SIZE * 2 + 5), dek=sealed["dek"], storage_key=other_key
        )
        _, victim_frames = frames_of(sealed["blob"])
        _, thief_frames = frames_of(other)
        victim_frames[1] = thief_frames[1]
        header, _ = frames_of(sealed["blob"])

        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(
                reassemble(header, victim_frames), dek=sealed["dek"], storage_key=sealed["key"]
            )

    def test_reordering_frames_is_detected(self, sealed):
        header, frames = frames_of(sealed["blob"])
        frames[0], frames[1] = frames[1], frames[0]

        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(
                reassemble(header, frames), dek=sealed["dek"], storage_key=sealed["key"]
            )

    def test_dropping_the_last_frame_is_detected(self, sealed):
        """Truncation at a frame boundary — the case a length check cannot catch.

        Every frame that survives authenticates, and the file is structurally
        valid. Only the final-frame flag makes this detectable.
        """
        header, frames = frames_of(sealed["blob"])

        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(
                reassemble(header, frames[:-1]), dek=sealed["dek"], storage_key=sealed["key"]
            )

    def test_truncating_inside_a_frame_is_detected(self, sealed):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(sealed["blob"][:-30], dek=sealed["dek"], storage_key=sealed["key"])

    def test_appending_a_frame_after_the_final_one_is_detected(self, sealed):
        """Extension, the mirror of truncation.

        A frame appended after the end could otherwise add content to a document
        someone has already signed off on.
        """
        header, frames = frames_of(sealed["blob"])

        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(
                reassemble(header, [*frames, frames[-1]]),
                dek=sealed["dek"],
                storage_key=sealed["key"],
            )

    def test_a_partial_read_before_the_failure_is_still_a_failure(self, sealed):
        """The generator must raise, not stop quietly, on a bad tail frame.

        This is what makes the streaming download safe: the caller gets an
        exception mid-stream. Silently ending the iteration would produce a
        short file that looks complete.
        """
        header, frames = frames_of(sealed["blob"])
        produced = []

        with pytest.raises(crypto.DecryptionError):
            for frame in crypto.decrypt_stream(
                io.BytesIO(reassemble(header, frames[:-1])),
                dek=sealed["dek"],
                storage_key=sealed["key"],
            ):
                produced.append(frame)

        assert produced, "the frames before the missing one should have been yielded"

    def test_an_absurd_frame_length_does_not_become_an_allocation(self, sealed):
        """A corrupt length field is refused before it is used to read.

        Without the bound, four bytes of garbage on disk would ask for up to 4 GB.
        """
        header, frames = frames_of(sealed["blob"])
        blob = header + struct.pack(">I", 4_000_000_000) + frames[0]

        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(blob, dek=sealed["dek"], storage_key=sealed["key"])


class TestTheHeader:
    def test_something_that_is_not_ours_is_refused(self):
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(
                b"%PDF-1.7 plain and unencrypted", dek=crypto.generate_dek(), storage_key=uuid4()
            )

    def test_an_empty_file_is_refused(self):
        """What a failed restore leaves behind. It must not read as an empty document."""
        with pytest.raises(crypto.DecryptionError):
            crypto.decrypt_bytes(b"", dek=crypto.generate_dek(), storage_key=uuid4())

    def test_a_future_format_version_is_refused_by_name(self, sealed):
        """A version bump must fail loudly here rather than mis-parse.

        The message names the version, because the operator reading it needs to
        know they are running an older build against a newer store.
        """
        blob = bytearray(sealed["blob"])
        blob[len(crypto.MAGIC)] = crypto.VERSION + 1

        with pytest.raises(crypto.DecryptionError, match="version"):
            crypto.decrypt_bytes(bytes(blob), dek=sealed["dek"], storage_key=sealed["key"])
