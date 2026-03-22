"""Fernet-based symmetric encryption for storing Yahoo OAuth tokens."""
from __future__ import annotations

from cryptography.fernet import Fernet

from fantasai.config import settings


def _get_fernet() -> Fernet:
    key = settings.token_encryption_key
    if not key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set. "
            "Generate one with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a plaintext token string. Returns a base64-encoded ciphertext string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted token string. Returns the original plaintext."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()
