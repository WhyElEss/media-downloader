import base64
import hashlib
import hmac
import os
import secrets
import time

from fastapi import HTTPException, Request

from . import config, db

COOKIE = "mdl_session"
_SCRYPT_MAXMEM = 128 * 1024 * 1024


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# Format: scrypt:N:r:p:salt:hash — no "$", so it needs no escaping in .env.
def hash_password(password: str, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{_b64(salt)}:{_b64(key)}"


def _parse(stored: str):
    try:
        alg, n, r, p, salt, key = stored.split(":")
        if alg != "scrypt":
            return None
        return int(n), int(r), int(p), _unb64(salt), _unb64(key)
    except (ValueError, TypeError):
        return None


def password_configured() -> bool:
    return _parse(config.PASSWORD_HASH) is not None


# Used when the username is wrong so both failure paths cost the same time.
_DUMMY = hash_password(secrets.token_hex(8))


def check_credentials(username: str, password: str) -> bool:
    parsed = _parse(config.PASSWORD_HASH)
    user_ok = hmac.compare_digest(username.encode(), config.USERNAME.encode())
    if parsed is None or not user_ok:
        parsed = _parse(_DUMMY)
    n, r, p, salt, key = parsed
    got = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MAXMEM, dklen=len(key))
    return hmac.compare_digest(got, key) and user_ok and password_configured()


class LoginLimiter:
    """Failed logins per client IP, plus a global ceiling that also stops
    attackers who rotate IPs or spoof X-Forwarded-For."""

    def __init__(self):
        self.window = config.LOGIN_LOCK_MINUTES * 60
        self.per_ip = config.LOGIN_MAX_ATTEMPTS
        self.total = config.LOGIN_MAX_ATTEMPTS * 4
        self.fails: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        now = time.time()
        recent = [t for t in self.fails.get(key, []) if now - t < self.window]
        self.fails[key] = recent
        return recent

    def retry_after(self, ip: str) -> int:
        """Seconds until a login attempt is allowed again; 0 if allowed now."""
        now = time.time()
        for key, limit in (("ip:" + ip, self.per_ip), ("all", self.total)):
            recent = self._recent(key)
            if len(recent) >= limit:
                return int(self.window - (now - recent[-limit])) + 1
        return 0

    def failed(self, ip: str) -> None:
        now = time.time()
        for key in ("ip:" + ip, "all"):
            self._recent(key).append(now)

    def succeeded(self, ip: str) -> None:
        self.fails.pop("ip:" + ip, None)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session() -> tuple[str, str]:
    now = time.time()
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    db.execute(
        "INSERT INTO sessions(token, csrf, created, expires) VALUES (?, ?, ?, ?)",
        (_digest(token), csrf, now, now + config.SESSION_DAYS * 86400),
    )
    return token, csrf


def get_session(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    row = db.one("SELECT * FROM sessions WHERE token = ?", (_digest(token),))
    if not row or row["expires"] < time.time():
        return None
    return row


def delete_session(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.execute("DELETE FROM sessions WHERE token = ?", (_digest(token),))


def cookie_secure(request: Request) -> bool:
    if config.COOKIE_SECURE in ("true", "1", "yes"):
        return True
    if config.COOKIE_SECURE in ("false", "0", "no"):
        return False
    # uvicorn --proxy-headers rewrites the scheme from X-Forwarded-Proto.
    return request.url.scheme == "https"


def require_session(request: Request) -> dict:
    session = get_session(request)
    if not session:
        raise HTTPException(401, "Not signed in")
    return session


def check_csrf(session: dict, token: str | None) -> None:
    if not token or not hmac.compare_digest(token, session["csrf"]):
        raise HTTPException(403, "Invalid CSRF token, reload the page")


def require_csrf(request: Request) -> dict:
    session = require_session(request)
    check_csrf(session, request.headers.get("x-csrf-token"))
    return session
