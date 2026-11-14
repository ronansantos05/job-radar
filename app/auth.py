"""Senhas (scrypt), sessões por cookie e limite de tentativas de login."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

SESSION_COOKIE = "radar_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")

# tentativas de login com falha: chave -> (quantidade, início da janela)
_failures: dict[str, tuple[int, float]] = {}
MAX_FAILURES = 6
WINDOW_SECONDS = 600


def valid_email(email: str) -> bool:
    return bool(_EMAIL.match(email))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """O banco guarda só o hash: quem ler o banco não consegue usar as sessões."""
    return hashlib.sha256(token.encode()).hexdigest()


def blocked(key: str) -> bool:
    count, start = _failures.get(key, (0, 0.0))
    if time.time() - start > WINDOW_SECONDS:
        _failures.pop(key, None)
        return False
    return count >= MAX_FAILURES


def record_failure(key: str) -> None:
    count, start = _failures.get(key, (0, time.time()))
    if time.time() - start > WINDOW_SECONDS:
        count, start = 0, time.time()
    _failures[key] = (count + 1, start)
    if len(_failures) > 5000:  # não deixa a memória crescer sem limite
        _failures.clear()


def clear_failures(key: str) -> None:
    _failures.pop(key, None)


def invite_code() -> str:
    """Código exigido no cadastro. DASH_PASSWORD é aceito por compatibilidade com a versão antiga."""
    return os.getenv("SIGNUP_CODE") or os.getenv("DASH_PASSWORD") or ""
