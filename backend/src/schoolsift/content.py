from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path
from typing import Protocol

import keyring
from cryptography.fernet import Fernet, InvalidToken


class ContentStore(Protocol):
    def put(self, household_id: str, content: bytes) -> str: ...

    def get(self, household_id: str, ref: str) -> bytes: ...

    def delete(self, household_id: str, ref: str) -> None: ...


class LocalEncryptedContentStore:
    SERVICE = "schoolsift"
    KEY_NAME = "content-master-key"

    def __init__(self, directory: Path, *, key: bytes | None = None) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self._directory, 0o700)
        self._fernet = Fernet(key if key is not None else self._master_key())

    def _master_key(self) -> bytes:
        raw = keyring.get_password(self.SERVICE, self.KEY_NAME)
        if raw is None:
            raw = Fernet.generate_key().decode()
            keyring.set_password(self.SERVICE, self.KEY_NAME, raw)
        return raw.encode()

    def _path(self, ref: str) -> Path:
        path = self._directory / ref
        if path.parent != self._directory:
            raise KeyError(f"Invalid content ref: {ref}")
        return path

    def _open(self, household_id: str, ref: str) -> bytes:
        try:
            sealed = self._path(ref).read_bytes()
        except FileNotFoundError as e:
            raise KeyError(f"Unknown content ref: {ref}") from e
        try:
            plain = self._fernet.decrypt(sealed)
        except InvalidToken as e:
            raise KeyError(f"Content failed integrity check: {ref}") from e
        owner, _, content = plain.partition(b"\x00")
        if owner.decode() != household_id:
            raise PermissionError(f"Content {ref} belongs to another household.")
        return content

    def put(self, household_id: str, content: bytes) -> str:
        ref = secrets.token_urlsafe(24)
        path = self._path(ref)
        path.write_bytes(
            self._fernet.encrypt(household_id.encode() + b"\x00" + content)
        )
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return ref

    def get(self, household_id: str, ref: str) -> bytes:
        return self._open(household_id, ref)

    def delete(self, household_id: str, ref: str) -> None:
        self._open(household_id, ref)
        self._path(ref).unlink()
