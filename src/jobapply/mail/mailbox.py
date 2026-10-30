"""The user's mailbox (Gmail app password, Outlook, Yahoo or any IMAP/SMTP pair): provider presets, saving
the credentials into the Vault, and a connection test that logs in, reads the newest subject and tries an
SMTP login without sending anything.

What is stored where:
- Vault site "mailbox", kind "imap"  -> {host, port, password}, username = the address
- Vault site "mailbox", kind "smtp"  -> {host, port, password}, username = the address
- users.profile["mailbox"]            -> provider, address, hosts, last_test_at, status, error, newest subject
  (never the password; the profile is plain JSON)

The Settings card, the onboarding step and the `jobapply mail` CLI all go through `save_mailbox` and
`test_mailbox`. Nothing here sends mail or changes flags on the server: the test uses BODY.PEEK and the
SMTP step stops after `login`.
"""

from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlmodel import Session

from jobapply.logs import get_logger
from jobapply.mail.imap import ImapReader, MailboxConfig
from jobapply.mail.imap import _default_client as _imap_client
from jobapply.mail.otp import mask_digits
from jobapply.mail.smtp import SmtpConfig
from jobapply.mail.smtp import _default_client as _smtp_client
from jobapply.models import User, utcnow
from jobapply.vault import Vault, VaultError

log = get_logger(__name__)

SITE = "mailbox"
STATUSES = ("ok", "imap_failed", "smtp_failed", "untested", "missing", "unreadable")

# provider -> (label, imap host, imap port, smtp host, smtp port, hint shown under the form)
PROVIDERS: dict[str, dict[str, Any]] = {
    "gmail": {
        "label": "Gmail / Google Workspace",
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "hint": "Needs a 16-character app password (Google Account → Security → 2-Step Verification → App "
        "passwords). Your normal password will not work and is never wanted here.",
    },
    "outlook": {
        "label": "Outlook / Hotmail / Microsoft 365",
        "imap_host": "outlook.office365.com",
        "imap_port": 993,
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "hint": "Personal accounts: an app password from account.microsoft.com → Security. Work accounts may "
        "have IMAP disabled by the admin.",
    },
    "yahoo": {
        "label": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com",
        "imap_port": 993,
        "smtp_host": "smtp.mail.yahoo.com",
        "smtp_port": 465,
        "hint": "Generate an app password under Account Security → Generate app password.",
    },
    "custom": {
        "label": "Custom IMAP / SMTP",
        "imap_host": "",
        "imap_port": 993,
        "smtp_host": "",
        "smtp_port": 587,
        "hint": "Any mailbox that speaks IMAP over SSL (993) and SMTP with STARTTLS (587) or SSL (465).",
    },
}


def provider_for(email: str) -> str:
    """A sensible preset for an address, used when the form posts no provider."""
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain in ("gmail.com", "googlemail.com"):
        return "gmail"
    if domain in ("outlook.com", "hotmail.com", "live.com", "msn.com", "office365.com"):
        return "outlook"
    if domain.endswith("yahoo.com") or domain in ("ymail.com", "rocketmail.com"):
        return "yahoo"
    return "custom"


@dataclass
class MailboxSettings:
    """What the forms post. Hosts/ports default from the provider preset when left blank."""

    provider: str = "gmail"
    email: str = ""
    password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    smtp_host: str = ""
    smtp_port: int = 587

    @classmethod
    def from_form(cls, form: dict[str, Any]) -> MailboxSettings:
        email = str(form.get("email") or form.get("username") or "").strip()
        provider = str(form.get("provider") or "").strip().lower() or provider_for(email)
        if provider not in PROVIDERS:
            provider = "custom"
        preset = PROVIDERS[provider]

        def _port(key: str, default: int) -> int:
            raw = str(form.get(key) or "").strip()
            try:
                return int(raw) if raw else int(default)
            except ValueError as exc:
                raise ValueError(f"{key.replace('_', ' ')} must be a number") from exc

        return cls(
            provider=provider,
            email=email,
            password=str(form.get("password") or form.get("secret") or ""),
            imap_host=str(form.get("imap_host") or "").strip().lower() or preset["imap_host"],
            imap_port=_port("imap_port", preset["imap_port"]),
            smtp_host=str(form.get("smtp_host") or "").strip().lower() or preset["smtp_host"],
            smtp_port=_port("smtp_port", preset["smtp_port"]),
        )

    def validate(self) -> None:
        if "@" not in self.email or " " in self.email:
            raise ValueError("Enter the mailbox address (you@example.com)")
        if not self.imap_host:
            raise ValueError("IMAP host is required for a custom provider")
        if not self.smtp_host:
            raise ValueError("SMTP host is required for a custom provider")
        if not (0 < self.imap_port < 65536 and 0 < self.smtp_port < 65536):
            raise ValueError("Ports must be between 1 and 65535")


def _public(info: dict[str, Any] | None) -> dict[str, Any]:
    """The profile entry without anything secret (defensive: the password is never written there)."""
    info = dict(info or {})
    info.pop("password", None)
    return info


def mailbox_status(session: Session, user: User) -> dict[str, Any]:
    """What the status chip and the forms need: provider, address, hosts, last test, status. Reads the
    Vault only for the address and hosts; the password stays in the ciphertext."""
    info = _public((user.profile or {}).get("mailbox"))
    try:
        vault: Vault | None = Vault()
    except VaultError:
        vault = None
    imap = smtp = None
    unreadable = False
    if vault is not None:
        try:
            imap = vault.load(session, user.id, SITE, "imap")
            smtp = vault.load(session, user.id, SITE, "smtp")
        except VaultError as exc:
            # Saved under another JOBAPPLY_VAULT_KEY (rotated key, DB restored elsewhere): the page must still
            # render so the user can remove / re-enter the mailbox; nothing here needs the plaintext.
            log.warning("mailbox credentials unreadable", user=user.username, error=str(exc)[:120])
            unreadable = True
    configured = bool(imap and imap.get("host")) or unreadable
    out = {
        "configured": configured,
        "vault_ready": vault is not None,
        "email": (imap or {}).get("username") or info.get("email", ""),
        "provider": info.get("provider") or (provider_for((imap or {}).get("username", "")) if imap else "gmail"),
        "imap_host": (imap or {}).get("host", "") or info.get("imap_host", ""),
        "imap_port": int((imap or {}).get("port") or info.get("imap_port") or 993),
        "smtp_host": (smtp or {}).get("host", "") or info.get("smtp_host", ""),
        "smtp_port": int((smtp or {}).get("port") or info.get("smtp_port") or 587),
        "status": info.get("status") or ("untested" if configured else "missing"),
        "last_test_at": info.get("last_test_at", ""),
        "error": info.get("error", ""),
        "newest_subject": info.get("newest_subject", ""),
    }
    if unreadable:
        out["status"] = "unreadable"
        out["error"] = "saved with a different vault key: remove the mailbox and add it again"
    elif not configured and out["status"] not in ("missing",):
        out["status"] = "missing"
    return out


def status_chip(status: str) -> str:
    """CSS chip colour for a mailbox status."""
    return {"ok": "green", "untested": "amber", "missing": "gray"}.get(status, "red")


def save_mailbox(session: Session, user: User, settings: MailboxSettings) -> dict[str, Any]:
    """Store the IMAP and SMTP credentials (one Vault row each) and remember the public bits on the profile.
    An empty password keeps the one already saved, so hosts can be corrected without retyping it."""
    settings.validate()
    vault = Vault()  # raises VaultError when the server has no key; the caller flashes it
    password = settings.password
    if not password:
        existing = vault.load(session, user.id, SITE, "imap") or vault.load(session, user.id, SITE, "smtp") or {}
        password = str(existing.get("password") or "")
    if not password:
        raise ValueError("App password is required")
    vault.store(
        session, user.id, SITE, "imap", settings.email,
        {"password": password, "host": settings.imap_host, "port": str(settings.imap_port)},
    )
    vault.store(
        session, user.id, SITE, "smtp", settings.email,
        {"password": password, "host": settings.smtp_host, "port": str(settings.smtp_port)},
    )
    profile = dict(user.profile or {})
    previous = _public(profile.get("mailbox"))
    changed = any(
        previous.get(k) != v
        for k, v in (("email", settings.email), ("imap_host", settings.imap_host), ("smtp_host", settings.smtp_host))
    )
    if not str(profile.get("email") or "").strip():
        # The mailbox address doubles as the application email until the user fills one in on Profile.
        profile["email"] = settings.email
    profile["mailbox"] = {
        **previous,
        "provider": settings.provider,
        "email": settings.email,
        "imap_host": settings.imap_host,
        "imap_port": settings.imap_port,
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "saved_at": utcnow().isoformat(),
        # a new password or a new host invalidates the old test result
        "status": "untested" if (settings.password or changed or not previous.get("status")) else previous["status"],
    }
    if settings.password or changed:
        profile["mailbox"].pop("error", None)
    user.profile = profile
    session.add(user)
    session.commit()
    log.info("mailbox saved", user=user.username, provider=settings.provider, imap=settings.imap_host)
    return _public(profile["mailbox"])


def clear_mailbox(session: Session, user: User) -> int:
    """Delete both credential rows and the profile entry. Returns how many rows went."""
    from jobapply.models import Credential
    from jobapply.repo import UserScope

    scope = UserScope(session, user)
    gone = 0
    for kind in ("imap", "smtp"):
        cred: Credential | None = scope.credential(SITE, kind)
        if cred is not None:
            session.delete(cred)
            gone += 1
    profile = dict(user.profile or {})
    profile.pop("mailbox", None)
    user.profile = profile
    session.add(user)
    session.commit()
    return gone


# --------------------------------------------------------------------------- connection test

ImapFactory = Callable[[MailboxConfig], Any]
SmtpFactory = Callable[[SmtpConfig], Any]
# Swapped by tests for fakes; production uses the real imaplib / smtplib clients.
default_imap_factory: ImapFactory = _imap_client
default_smtp_factory: SmtpFactory = _smtp_client


@dataclass
class MailboxTest:
    status: str = "untested"  # ok | imap_failed | smtp_failed | missing
    imap_ok: bool = False
    smtp_ok: bool = False
    newest_subject: str = ""
    newest_from: str = ""
    newest_at: str = ""
    message_count: int = 0
    error: str = ""
    tested_at: str = field(default_factory=lambda: utcnow().isoformat())

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if self.ok:
            tail = f"; newest mail: {self.newest_subject[:80]!r}" if self.newest_subject else "; the inbox is empty"
            return f"Mailbox works: IMAP and SMTP logins succeeded{tail}."
        if self.status == "missing":
            return "No mailbox saved yet."
        where = "IMAP" if self.status == "imap_failed" else "SMTP"
        return f"{where} login failed: {self.error or 'unknown error'}"


def _explain(exc: BaseException) -> str:
    """One line for the UI, without echoing anything the server may have reflected back (never the password)."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    text = text.replace("b'", "").replace("'", "")
    if isinstance(exc, socket.gaierror):
        return "host not found (check the IMAP/SMTP host)"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "connection timed out (firewall or wrong port?)"
    if isinstance(exc, ssl.SSLError):
        return f"TLS error: {text[:120]}"
    if isinstance(exc, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)):
        low = text.lower()
        if "application-specific password" in low or "app password" in low or "authenticationfailed" in low:
            return "login rejected: use an app password (2-Step Verification must be on), not the account password"
        if "invalid credentials" in low or "535" in low or "authentication failed" in low:
            return "login rejected: wrong address or app password"
        return text[:160]
    return f"{type(exc).__name__}: {text[:140]}"


def test_mailbox(
    session: Session,
    user: User,
    imap_factory: ImapFactory | None = None,
    smtp_factory: SmtpFactory | None = None,
    record: bool = True,
) -> MailboxTest:
    """Log in over IMAP, read the newest message's header (subject, sender, date; BODY.PEEK so nothing is
    marked read), then log in over SMTP and quit without sending. Never raises on a bad mailbox: the
    outcome lands in `users.profile["mailbox"]` (`last_test_at`, `status`, `error`, `newest_subject`)."""
    from jobapply.mail.inbox import mailbox_configs

    imap_cfg, smtp_cfg = mailbox_configs(session, user)
    result = MailboxTest()
    if imap_cfg is None:
        result.status = "missing"
        result.error = "no mailbox credentials saved"
        return _record(session, user, result) if record else result

    reader = ImapReader(imap_cfg, client_factory=imap_factory or default_imap_factory)
    try:
        newest = reader.newest(limit=1)
        result.imap_ok = True
        if newest:
            m = newest[0]
            # The newest mail may be an OTP whose code sits in the subject: masked before it is stored on the
            # plain-JSON profile and rendered on the Settings / onboarding pages.
            result.newest_subject = mask_digits(m.subject)[:200]
            result.newest_from = m.sender
            result.newest_at = m.received_at.isoformat()
            result.message_count = int(m.uid) if m.uid.isdigit() else 1
    except Exception as exc:  # noqa: BLE001 - any login / network / parse trouble is the test's result
        result.status = "imap_failed"
        result.error = _explain(exc)
        log.warning("mailbox imap test failed", user=user.username, host=imap_cfg.host, error=result.error)
        return _record(session, user, result) if record else result
    finally:
        reader.close()

    if smtp_cfg is None:
        result.status = "smtp_failed"
        result.error = "no SMTP host"
        return _record(session, user, result) if record else result
    client = None
    try:
        client = (smtp_factory or default_smtp_factory)(smtp_cfg)  # connects, STARTTLS, logs in; sends nothing
        result.smtp_ok = True
        result.status = "ok"
    except Exception as exc:  # noqa: BLE001
        result.status = "smtp_failed"
        result.error = _explain(exc)
        log.warning("mailbox smtp test failed", user=user.username, host=smtp_cfg.host, error=result.error)
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                pass
    return _record(session, user, result) if record else result


def _record(session: Session, user: User, result: MailboxTest) -> MailboxTest:
    profile = dict(user.profile or {})
    info = _public(profile.get("mailbox"))
    info.update(
        {
            "last_test_at": result.tested_at,
            "status": result.status,
            "error": result.error,
            "newest_subject": result.newest_subject,
            "imap_ok": result.imap_ok,
            "smtp_ok": result.smtp_ok,
        }
    )
    profile["mailbox"] = info
    user.profile = profile
    session.add(user)
    session.commit()
    return result


__all__ = [
    "PROVIDERS",
    "SITE",
    "STATUSES",
    "MailboxSettings",
    "MailboxTest",
    "clear_mailbox",
    "mailbox_status",
    "provider_for",
    "save_mailbox",
    "status_chip",
    "test_mailbox",
]
