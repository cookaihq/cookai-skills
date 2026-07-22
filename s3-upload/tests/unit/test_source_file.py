import os

import pytest

from source_file import SourceError, VerifiedSource, verify_resumable_source


def test_single_put_uses_open_descriptor_and_verifies_hash(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"original")
    with VerifiedSource.open(str(path), soft_max_bytes=100) as source:
        assert source.snapshot.size == 8
        assert source.snapshot.sha256 == "0682c5f2076f099c34cfdd15a9e063849ed437a49677e6fcc5b4198c76575be5"
        assert source.single_put_bytes() == b"original"


def test_path_swap_after_open_does_not_change_bytes(tmp_path):
    path = tmp_path / "source.bin"
    replacement = tmp_path / "replacement.bin"
    path.write_bytes(b"safe")
    replacement.write_bytes(b"sensitive")
    with VerifiedSource.open(str(path), soft_max_bytes=100) as source:
        path.unlink()
        replacement.rename(path)
        assert source.single_put_bytes() == b"safe"


def test_in_place_change_after_initial_hash_is_rejected(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"first")
    with VerifiedSource.open(str(path), soft_max_bytes=100) as source:
        with open(path, "r+b") as changed:
            changed.write(b"other")
            changed.flush()
            os.fsync(changed.fileno())
        with pytest.raises(SourceError, match="changed"):
            source.single_put_bytes()


def test_soft_limit_rejects_before_body_is_returned(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"123")
    with pytest.raises(SourceError, match="soft size"):
        VerifiedSource.open(str(path), soft_max_bytes=2)


def test_part_ranges_hash_exact_bytes_and_resume_verifies_acknowledged_parts(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"abcdefghij")
    with VerifiedSource.open(str(path), soft_max_bytes=100) as source:
        parts = list(source.parts(4))
        assert [(part.number, part.offset, part.data) for part in parts] == [
            (1, 0, b"abcd"), (2, 4, b"efgh"), (3, 8, b"ij")
        ]
        acknowledged = [part.as_checkpoint() | {"etag": f"etag-{part.number}"} for part in parts[:2]]
        verify_resumable_source(source.snapshot.as_checkpoint(), acknowledged, part_size_bytes=4)


def test_resume_rejects_changed_file_or_part_hash(tmp_path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"abcdefgh")
    with VerifiedSource.open(str(path), soft_max_bytes=100) as source:
        snapshot = source.snapshot.as_checkpoint()
        acknowledged = [next(source.parts(4)).as_checkpoint() | {"etag": "etag-1"}]
    path.write_bytes(b"abcdWXYZ")
    with pytest.raises(SourceError, match="changed"):
        verify_resumable_source(snapshot, acknowledged, part_size_bytes=4)

    path.write_bytes(b"abcdefgh")
    with VerifiedSource.open(str(path), soft_max_bytes=100) as source:
        snapshot = source.snapshot.as_checkpoint()
        acknowledged = [next(source.parts(4)).as_checkpoint() | {"etag": "etag-1"}]
    acknowledged[0]["sha256"] = "0" * 64
    with pytest.raises(SourceError, match="part"):
        verify_resumable_source(snapshot, acknowledged, part_size_bytes=4)
