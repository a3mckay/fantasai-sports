"""Firebase ID token verification using Google's public keys.

Firebase ID tokens are RS256-signed JWTs. We verify them without firebase-admin
by fetching Google's public keys directly — no service account required.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from fantasai.config import settings

_log = logging.getLogger(__name__)

_CERTS_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
_ISSUER_PREFIX = "https://securetoken.google.com/"

# Simple in-memory cache: (certs_dict, fetched_at_timestamp)
_cert_cache: tuple[dict[str, str], float] | None = None
_CACHE_TTL = 3600  # 1 hour


def _cert_pem_to_public_key(cert_pem: str) -> str:
    """Extract the RSA public key (SPKI/PEM) from an X.509 certificate string."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return cert.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


def _fetch_google_certs() -> dict[str, str]:
    """Fetch and cache Firebase signing certificates from Google."""
    global _cert_cache
    now = time.time()
    if _cert_cache and (now - _cert_cache[1]) < _CACHE_TTL:
        return _cert_cache[0]

    resp = httpx.get(_CERTS_URL, timeout=10)
    resp.raise_for_status()
    certs = resp.json()
    _cert_cache = (certs, now)
    _log.debug("Refreshed Firebase public key cache (%d keys)", len(certs))
    return certs


def verify_firebase_token(id_token: str) -> dict[str, Any]:
    """Verify a Firebase ID token and return its decoded claims.

    Raises:
        jwt.PyJWTError / httpx.HTTPError: on invalid token or network failure.
        ValueError: if token claims are structurally invalid.
    """
    project_id = settings.firebase_project_id
    expected_issuer = f"{_ISSUER_PREFIX}{project_id}"

    # Decode header to get the key ID (kid) — don't verify yet
    header = jwt.get_unverified_header(id_token)
    kid = header.get("kid")
    if not kid:
        raise ValueError("Firebase token missing 'kid' header")

    certs = _fetch_google_certs()
    cert_pem = certs.get(kid)
    if not cert_pem:
        # Key not in cache — refresh once and retry
        global _cert_cache
        _cert_cache = None
        certs = _fetch_google_certs()
        cert_pem = certs.get(kid)
        if not cert_pem:
            raise ValueError(f"Firebase token 'kid' {kid!r} not found in Google certs")

    # Extract the RSA public key from the X.509 certificate so PyJWT can
    # verify the RS256 signature without ambiguity about certificate format.
    public_key_pem = _cert_pem_to_public_key(cert_pem)

    claims: dict[str, Any] = jwt.decode(
        id_token,
        public_key_pem,
        algorithms=["RS256"],
        audience=project_id,
        issuer=expected_issuer,
        options={"verify_exp": True},
    )

    # Additional Firebase-specific validations
    if not claims.get("sub"):
        raise ValueError("Firebase token missing 'sub' (uid) claim")
    if claims.get("auth_time", 0) > time.time():
        raise ValueError("Firebase token auth_time is in the future")

    return claims
