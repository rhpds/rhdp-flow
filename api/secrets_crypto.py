"""At-rest encryption for sensitive workspace fields (schedule passwords).

Uses Fernet (symmetric). Key from ``RHDP_FERNET_KEY`` (url-safe base64 32-byte
key) or, for local/dev only, a stable derivation from ``RHDP_API_KEY``.

When neither is set, values are stored with a ``plain:`` prefix (dev only).
Set ``RHDP_REQUIRE_SECRET_KEY=true`` in production to refuse plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

logger = logging.getLogger("rhdp_flow.api")

_PLAIN_PREFIX = "plain:"
_ENC_PREFIX = "enc:"


def _fernet():
    raw = (os.environ.get("RHDP_FERNET_KEY") or "").strip()
    if not raw:
        api_key = (os.environ.get("RHDP_API_KEY") or "").strip()
        if not api_key:
            return None
        # Deterministic 32-byte key from API key for local/dev convenience.
        digest = hashlib.sha256(f"rhdp-flow-fernet:{api_key}".encode()).digest()
        raw = base64.urlsafe_b64encode(digest).decode()
    try:
        from cryptography.fernet import Fernet

        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fernet init failed: %s", exc)
        return None


def require_secret_key() -> bool:
    return os.environ.get("RHDP_REQUIRE_SECRET_KEY", "").strip().lower() in ("1", "true", "yes")


def encrypt_str(value: str) -> str:
    if value is None:
        return ""
    if not value:
        return ""
    if value.startswith(_ENC_PREFIX) or value.startswith(_PLAIN_PREFIX):
        return value
    f = _fernet()
    if f is None:
        if require_secret_key():
            raise RuntimeError("RHDP_FERNET_KEY (or RHDP_API_KEY) required when RHDP_REQUIRE_SECRET_KEY=true")
        return f"{_PLAIN_PREFIX}{value}"
    token = f.encrypt(value.encode("utf-8")).decode("ascii")
    return f"{_ENC_PREFIX}{token}"


def decrypt_str(value: str) -> str:
    if not value:
        return ""
    if value.startswith(_PLAIN_PREFIX):
        return value[len(_PLAIN_PREFIX) :]
    if not value.startswith(_ENC_PREFIX):
        # Legacy plaintext row
        return value
    token = value[len(_ENC_PREFIX) :]
    f = _fernet()
    if f is None:
        raise RuntimeError("Cannot decrypt: Fernet key not configured")
    return f.decrypt(token.encode("ascii")).decode("utf-8")
