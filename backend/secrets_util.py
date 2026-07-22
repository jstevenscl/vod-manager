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
