"""Access control for station (iPad) pages (issue #27).

A station can be *secured*: its public page/API/websocket then need the
station's `access_key` (rotating it revokes every old link) and, if a PIN is
set, a short-lived unlock token obtained by presenting key + PIN. Stations
without a key keep working as before (the slug alone is the credential).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from backend.core.settings import settings

PIN_MIN_LEN = 4
PIN_MAX_LEN = 8
TOKEN_TTL_SECONDS = 12 * 3600
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def new_access_key() -> str:
    return secrets.token_urlsafe(16)


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, **_SCRYPT)
    return f"{salt.hex()}${digest.hex()}"


def verify_pin(pin: str, stored: str | None) -> bool:
    if not stored or "$" not in stored:
        return False
    salt_hex, digest_hex = stored.split("$", 1)
    try:
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


def _sign(payload: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(slug: str, key_version: int, *, now: float | None = None) -> str:
    expires = int((time.time() if now is None else now) + TOKEN_TTL_SECONDS)
    payload = f"{slug}.{key_version}.{expires}"
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _sign(payload)


def verify_token(token: str | None, slug: str, key_version: int, *, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        parts = payload.rsplit(".", 2)
        if len(parts) != 3 or not hmac.compare_digest(_sign(payload), signature):
            return False
        token_slug, version, expires = parts
        return token_slug == slug and int(version) == key_version and int(expires) >= (time.time() if now is None else now)
    except (ValueError, UnicodeDecodeError):
        return False


def check_access(station, key: str | None, token: str | None) -> str | None:
    """None when allowed, else what is missing: "key" or "pin"."""
    if station.access_key is None:
        return None
    if not key or not hmac.compare_digest(key.encode(), station.access_key.encode()):
        return "key"
    if station.pin_hash and not verify_token(token, station.slug, station.key_version):
        return "pin"
    return None
