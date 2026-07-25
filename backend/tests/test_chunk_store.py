import pytest

from app.errors import UploadIncomplete
from app.services.chunk_store import ChunkStore


def test_assemble_out_of_order(storage_root, tmp_path):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 2, b"ccc")
    store.save_chunk("u1", 0, b"aaa")
    store.save_chunk("u1", 1, b"bbb")

    dest = tmp_path / "out.bin"
    written = store.assemble("u1", 3, dest)

    assert written == 9
    assert dest.read_bytes() == b"aaabbbccc"


def test_received_indices(storage_root):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 5, b"x")
    store.save_chunk("u1", 0, b"y")

    assert store.received_indices("u1") == {0, 5}


def test_received_indices_unknown_upload(storage_root):
    store = ChunkStore(storage_root / "uploads")
    assert store.received_indices("nope") == set()


def test_bytes_received(storage_root):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"abcd")
    store.save_chunk("u1", 3, b"xy")

    assert store.bytes_received("u1") == 6
    assert store.bytes_received("nope") == 0


def test_duplicate_chunk_is_idempotent(storage_root, tmp_path):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"first")
    store.save_chunk("u1", 0, b"SECOND")

    dest = tmp_path / "out.bin"
    store.assemble("u1", 1, dest)

    assert dest.read_bytes() == b"SECOND"


def test_assemble_missing_chunk_raises(storage_root, tmp_path):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"aaa")

    with pytest.raises(UploadIncomplete) as exc:
        store.assemble("u1", 3, tmp_path / "out.bin")

    assert exc.value.code == "UPLOAD_INCOMPLETE"
    assert "1" in exc.value.message and "2" in exc.value.message


def test_purge_removes_all_chunks(storage_root):
    store = ChunkStore(storage_root / "uploads")
    store.save_chunk("u1", 0, b"aaa")
    store.purge("u1")

    assert store.received_indices("u1") == set()
