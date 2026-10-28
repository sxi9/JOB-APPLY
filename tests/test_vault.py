import pytest

from jobapply.models import User
from jobapply.vault import Vault, VaultError, generate_key, mask


def test_roundtrip_and_storage_is_encrypted(session):
    u = User(username="a", password_hash="x")
    session.add(u)
    session.commit()
    v = Vault(generate_key())
    cred = v.store(session, u.id, "naukri", "login", "a@example.com", {"password": "hunter2"})
    session.commit()
    assert b"hunter2" not in cred.secret_blob
    assert v.load(session, u.id, "naukri") == {"username": "a@example.com", "password": "hunter2"}
    with pytest.raises(VaultError):
        Vault(generate_key()).load(session, u.id, "naukri")


def test_mask():
    assert mask("hunter2") == "hu•••r2"
    assert mask("") == ""
