from __future__ import annotations

import stat

import pytest
from cryptography.fernet import Fernet

from schoolsift.content import LocalEncryptedContentStore


@pytest.fixture()
def content(tmp_path):
    return LocalEncryptedContentStore(tmp_path / "content", key=Fernet.generate_key())


def test_roundtrip_and_opaque_ref(content, tmp_path):
    ref = content.put("hh-1", b"hello school")
    assert "/" not in ref and ".." not in ref
    assert content.get("hh-1", ref) == b"hello school"
    stored = (tmp_path / "content" / ref).read_bytes()
    assert b"hello school" not in stored
    assert b"hh-1" not in stored


def test_file_permissions(content, tmp_path):
    ref = content.put("hh-1", b"data")
    mode = stat.S_IMODE((tmp_path / "content" / ref).stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE((tmp_path / "content").stat().st_mode) == 0o700


def test_cross_household_rejected(content):
    ref = content.put("hh-1", b"secret")
    with pytest.raises(PermissionError):
        content.get("hh-2", ref)


def test_tampered_ciphertext_rejected(content, tmp_path):
    ref = content.put("hh-1", b"authentic")
    path = tmp_path / "content" / ref
    blob = bytearray(path.read_bytes())
    blob[-5] ^= 0xFF
    path.write_bytes(bytes(blob))
    with pytest.raises(KeyError):
        content.get("hh-1", ref)


def test_delete_and_unknown_ref(content):
    ref = content.put("hh-1", b"bye")
    content.delete("hh-1", ref)
    with pytest.raises(KeyError):
        content.get("hh-1", ref)
    with pytest.raises(KeyError):
        content.delete("hh-1", ref)


def test_keyring_master_key_created_once(tmp_path, monkeypatch):
    backing: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "keyring.get_password", lambda svc, user: backing.get((svc, user))
    )
    monkeypatch.setattr(
        "keyring.set_password",
        lambda svc, user, pw: backing.__setitem__((svc, user), pw),
    )
    a = LocalEncryptedContentStore(tmp_path / "c")
    b = LocalEncryptedContentStore(tmp_path / "c")
    ref = a.put("hh-1", b"shared key")
    assert b.get("hh-1", ref) == b"shared key"
    assert len(backing) == 1
