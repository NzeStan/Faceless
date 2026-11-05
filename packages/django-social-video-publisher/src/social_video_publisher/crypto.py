"""Secret encryption with rotation. The first configured key encrypts new values."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .conf import get_settings
from .errors import TokenDecryptionError


def _fernet() -> MultiFernet:
    return MultiFernet([Fernet(key) for key in get_settings().token_encryption_keys])


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise TokenDecryptionError("No configured key can decrypt this value.") from exc


def rotate(value: str) -> str:
    try:
        return _fernet().rotate(value.encode()).decode()
    except InvalidToken as exc:
        raise TokenDecryptionError("No configured key can decrypt this value.") from exc
