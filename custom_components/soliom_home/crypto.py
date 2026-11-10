"""Cryptographic helpers used by the Soliom cloud API."""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .const import AES_IV, APP_KEY, APP_SECRET


def md5_hex(value: str) -> str:
    """Return the lowercase MD5 hex digest of a string."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def api_signature(
    timestamp_ms: int,
    app_key: str = APP_KEY,
    app_secret: str = APP_SECRET,
) -> str:
    """Build the XM ``Signature`` header value."""
    return md5_hex(f"{timestamp_ms}{app_key}{app_secret}")


def encrypt_passphrase(password: str, app_secret: str = APP_SECRET) -> str:
    """Encrypt a password the way the Android app's ``AESUtil`` does.

    Key: 32 ASCII bytes of ``md5(app_secret)`` (AES-256).
    IV:  ``Xmitech_aes_veri``.
    Mode: CBC with PKCS#7 padding, output as lowercase hex.
    """
    key = md5_hex(app_secret).encode("ascii")
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(AES_IV)).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex()
