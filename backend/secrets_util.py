"""Encryption at rest for stored credentials -- provider passwords,
Dispatcharr API tokens, and XC client secrets are all real, working
credentials (not something we ever need a one-way hash of, since the app
has to send them back out to actually connect), so this uses reversible
Fernet encryption rather than hashing.

The key lives inside config.json (see config.get_or_create_encryption_key)
rather than its own file, so it rides along with config's existing
backup/restore/reset lifecycle instead of being a separate thing a restore
onto a fresh instance could silently leave behind.
"""

import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

import config

_fernet_instance: Fernet | None = None


def _fernet() -> Fernet:
    global _fernet_instance
    if _fernet_instance is None:
        _fernet_instance = Fernet(config.get_or_create_encryption_key())
    return _fernet_instance


def encrypt_value(plaintext: str | None) -> str | None:
    if not plaintext:
        return plaintext
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(value: str | None) -> str | None:
    """Falls back to returning the raw value on InvalidToken -- covers rows
    written before encryption existed, so upgrading doesn't break existing
    connections. See vod_db._migrate_encrypt_plaintext_credentials for
    upgrading them to actually be encrypted."""
    if not value:
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return value


def is_encrypted(value: str | None) -> bool:
    if not value:
        return True  # nothing to migrate
    try:
        _fernet().decrypt(value.encode())
        return True
    except InvalidToken:
        return False


# PBKDF2-HMAC-SHA256, 260k iterations -- the same scheme/cost config.py's
# admin login uses (see its _hash_password), lifted out here so the portal
# login (backend/portal_auth.py, backend/portal_routes.py) shares one
# audited implementation instead of a second copy drifting out of sync.
# config.py's own admin-login code path is untouched and keeps calling its
# private _hash_password directly.
_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Returns (salt, hash). Pass an existing salt to verify against a known
    hash; omit it to generate a new salt for a brand-new password."""
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS).hex()
    return salt, hashed


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    _, candidate = hash_password(password, salt)
    return secrets.compare_digest(candidate.encode(), expected_hash.encode())
