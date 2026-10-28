"""Credential vault: secrets encrypted with Fernet before they touch the database.

The key lives in `JOBAPPLY_VAULT_KEY` on the server, never in the repo.
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import Session

from jobapply.config import get_settings
from jobapply.models import Credential, utcnow
from jobapply.repo import UserScope


class VaultError(RuntimeError):
    pass


def generate_key() -> str:
    return Fernet.generate_key().decode()


class Vault:
    def __init__(self, key: str | None = None):
        if key is None:
            secret = get_settings().vault_key
            key = secret.get_secret_value() if secret else None
        if not key:
            raise VaultError("JOBAPPLY_VAULT_KEY is not set; generate one with `jobapply vault-key`")
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, data: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(data).encode())

    def decrypt(self, blob: bytes) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(blob))
        except InvalidToken as exc:
            raise VaultError("cannot decrypt credential, wrong vault key?") from exc

    # convenience on top of the credentials table ------------------------------
    def store(self, session: Session, user_id: int, site: str, kind: str, username: str,
              secret: dict[str, Any]) -> Credential:
        scope = UserScope(session, user_id)
        cred = scope.credential(site, kind) or Credential(user_id=user_id, site=site, kind=kind)
        cred.username = username
        cred.secret_blob = self.encrypt(secret)
        cred.updated_at = utcnow()
        session.add(cred)
        return cred

    def load(self, session: Session, user_id: int, site: str, kind: str = "login") -> dict[str, Any] | None:
        cred = UserScope(session, user_id).credential(site, kind)
        if cred is None:
            return None
        return {"username": cred.username, **self.decrypt(cred.secret_blob)}


def mask(value: str | None) -> str:
    if not value:
        return ""
    return value[:2] + "•" * max(len(value) - 4, 3) + value[-2:] if len(value) > 4 else "•" * len(value)
