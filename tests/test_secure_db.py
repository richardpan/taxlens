"""Tests for secure_db lock/unlock round-trip."""
from pathlib import Path

import pytest

from taxlens import secure_db


def test_lock_unlock_roundtrip(tmp_path: Path):
    db = tmp_path / "test.sqlite"
    payload = b"SQLite format 3\x00" + b"\x00" * 200 + b"-- some user data --"
    db.write_bytes(payload)

    blob = secure_db.lock("hunter2", db_path=db, iterations=10_000)
    assert blob.exists()
    assert not db.exists()
    assert secure_db.is_locked(db_path=db)

    restored = secure_db.unlock("hunter2", db_path=db)
    assert restored.exists()
    assert restored.read_bytes() == payload
    assert not blob.exists()
    assert not secure_db.is_locked(db_path=db)


def test_unlock_wrong_passphrase(tmp_path: Path):
    db = tmp_path / "test.sqlite"
    db.write_bytes(b"hello world")
    secure_db.lock("correct", db_path=db, iterations=10_000)
    with pytest.raises(ValueError, match="Wrong passphrase"):
        secure_db.unlock("wrong", db_path=db)


def test_lock_when_blob_exists_refuses(tmp_path: Path):
    db = tmp_path / "test.sqlite"
    db.write_bytes(b"hello")
    secure_db.lock("pw", db_path=db, iterations=10_000)
    # Restore plaintext but leave blob in place (simulate a half-broken state).
    secure_db.unlock("pw", db_path=db)
    secure_db.lock("pw", db_path=db, iterations=10_000)
    db.write_bytes(b"new plaintext")
    with pytest.raises(FileExistsError):
        secure_db.lock("pw", db_path=db, iterations=10_000)
