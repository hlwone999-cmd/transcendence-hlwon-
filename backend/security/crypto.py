"""Enkripsi kredensial MT5 — Fernet (authenticated encryption).

Fernet = AES-128-CBC + HMAC-SHA256 (encrypt-then-MAC): setara kategori
dengan AES-GCM untuk jaminan integritas+kerahasiaan (brief §D/§5: "AES-GCM
atau setara").

Aturan mutlak:
- Kunci HANYA dari env MT5_CRED_KEY (format: Fernet key 32-byte
  url-safe base64, `cryptography.fernet.Fernet.generate_key()`).
  Tidak pernah di database, tidak pernah di repository, tidak di-log.
- MT5_CRED_KEY_OLD (opsional): kunci lama saat rotasi — dipakai hanya
  untuk DEKRIPSI; simpan ulang kredensial dengan kunci baru saat akun
  disinkronkan/ubah. key_version mencatat indeks kunci (1=baru, 0=lama).
- Ciphertext tidak pernah dikembalikan ke klien API dan tidak di-log.
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger(__name__)

KEY_VERSION_NEW = 1
KEY_VERSION_OLD = 0


class CredentialCryptoError(Exception):
    """Kunci tidak tersedia / ciphertext rusak."""


def generate_key() -> str:
    """Utilitas admin: buat kunci baru utk env MT5_CRED_KEY."""
    return Fernet.generate_key().decode("ascii")


def _build(config) -> MultiFernet | None:
    keys = []
    if getattr(config, "cred_key", ""):
        try:
            keys.append(Fernet(config.cred_key.encode("ascii")))
        except Exception as e:  # kunci malformed -> gagal cepat & jelas
            raise CredentialCryptoError(
                "MT5_CRED_KEY bukan Fernet key valid: "
                f"{type(e).__name__}") from e
    if getattr(config, "cred_key_old", ""):
        try:
            keys.append(Fernet(config.cred_key_old.encode("ascii")))
        except Exception as e:
            raise CredentialCryptoError(
                f"MT5_CRED_KEY_OLD bukan Fernet key valid: "
                f"{type(e).__name__}") from e
    if not keys:
        return None
    return MultiFernet(keys)


def encrypt_password(config, plaintext: str) -> tuple[str, int]:
    """Enkripsi password MT5. Return (ciphertext, key_version)."""
    f = _build(config)
    if f is None:
        raise CredentialCryptoError(
            "Kredensial tidak bisa dienkripsi: MT5_CRED_KEY tidak diisi")
    if not plaintext:
        raise CredentialCryptoError("Password kosong ditolak")
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii"), KEY_VERSION_NEW


def decrypt_password(config, ciphertext: str, key_version: int = 1) -> str:
    """Dekripsi password MT5 (untuk provider connect — tidak di-log)."""
    f = _build(config)
    if f is None:
        raise CredentialCryptoError(
            "Kredensial tidak bisa didekripsi: MT5_CRED_KEY tidak diisi")
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise CredentialCryptoError(
            "Kredensial tidak bisa didekripsi: kunci tidak cocok "
            "(MT5_CRED_KEY/OLD salah atau ciphertext rusak)") from e
