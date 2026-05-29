"""At-rest encryption for the TaxLens SQLite file.

We don't link SQLCipher (no native build chain assumed on the user's machine).
Instead, when the DB is "locked", the SQLite file on disk is replaced by an
encrypted blob `taxlens.sqlite.enc`. On `unlock`, we decrypt back to the
plaintext path so the rest of the app can use a vanilla SQLite file.

Crypto:
  - Key derived via PBKDF2-HMAC-SHA256(passphrase, salt, iterations=480_000)
  - Symmetric encryption via Fernet (AES-128-CBC + HMAC-SHA256)
  - 16-byte random salt stored alongside the ciphertext in a single header

File layout of `taxlens.sqlite.enc`:
    magic(8)   = b"TAXLENS\x01"
    salt(16)
    iters(4, big-endian uint32)
    ciphertext (Fernet token)

This is enough to protect the file when the laptop is off / the user is logged
out. It is NOT a substitute for full-disk encryption against a live attacker
with code execution on the running machine.
"""
from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from taxlens.db import default_db_path

MAGIC = b"TAXLENS\x01"
SALT_LEN = 16
DEFAULT_ITERS = 480_000


@dataclass
class VaultPaths:
    plain: Path  # the working SQLite file
    blob: Path   # the encrypted-at-rest blob

    @classmethod
    def resolve(cls, db_path: Path | None = None) -> "VaultPaths":
        plain = (db_path or default_db_path()).resolve()
        return cls(plain=plain, blob=plain.with_suffix(plain.suffix + ".enc"))


def _derive_key(passphrase: str, salt: bytes, iterations: int = DEFAULT_ITERS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def is_locked(db_path: Path | None = None) -> bool:
    """True iff the encrypted blob exists and the plaintext does not."""
    p = VaultPaths.resolve(db_path)
    return p.blob.exists() and not p.plain.exists()


def lock(passphrase: str, db_path: Path | None = None, iterations: int = DEFAULT_ITERS) -> Path:
    """Encrypt the SQLite file in place, deleting the plaintext on success."""
    p = VaultPaths.resolve(db_path)
    if not p.plain.exists():
        raise FileNotFoundError(f"No plaintext DB at {p.plain} to lock.")
    if p.blob.exists():
        raise FileExistsError(
            f"Encrypted blob already at {p.blob}. Unlock first or remove it explicitly."
        )

    salt = os.urandom(SALT_LEN)
    key = _derive_key(passphrase, salt, iterations)
    token = Fernet(key).encrypt(p.plain.read_bytes())

    tmp = p.blob.with_suffix(p.blob.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(MAGIC)
        f.write(salt)
        f.write(struct.pack(">I", iterations))
        f.write(token)
    os.replace(tmp, p.blob)
    p.plain.unlink()
    return p.blob


def unlock(passphrase: str, db_path: Path | None = None) -> Path:
    """Decrypt back to the plaintext SQLite file. Wrong passphrase raises."""
    p = VaultPaths.resolve(db_path)
    if not p.blob.exists():
        raise FileNotFoundError(f"No encrypted blob at {p.blob}.")
    if p.plain.exists():
        raise FileExistsError(
            f"Plaintext DB still at {p.plain}. Already unlocked (or aborted lock)."
        )

    raw = p.blob.read_bytes()
    if not raw.startswith(MAGIC):
        raise ValueError(f"{p.blob} is not a TaxLens encrypted blob (bad magic).")
    header = len(MAGIC)
    salt = raw[header:header + SALT_LEN]
    iterations = struct.unpack(">I", raw[header + SALT_LEN:header + SALT_LEN + 4])[0]
    token = raw[header + SALT_LEN + 4:]

    key = _derive_key(passphrase, salt, iterations)
    try:
        plaintext = Fernet(key).decrypt(token)
    except InvalidToken as e:
        raise ValueError("Wrong passphrase (or the encrypted blob is corrupted).") from e

    tmp = p.plain.with_suffix(p.plain.suffix + ".tmp")
    tmp.write_bytes(plaintext)
    os.replace(tmp, p.plain)
    p.blob.unlink()
    return p.plain
