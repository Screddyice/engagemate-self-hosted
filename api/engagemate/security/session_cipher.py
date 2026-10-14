"""Versioned, account-bound encryption for Instagram session payloads."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any, Mapping
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SessionCipherError(ValueError):
    """A safe error for malformed or unauthenticated session envelopes."""


_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _decode_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_PATTERN.fullmatch(value):
        raise SessionCipherError("invalid session envelope")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as error:
        raise SessionCipherError("invalid session envelope") from error
    if _encode_base64url(decoded) != value:
        raise SessionCipherError("invalid session envelope")
    return decoded


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class SessionCipher:
    """Encrypt session JSON with AES-256-GCM bound to one Instagram account."""

    _VERSION = "v1"
    _NONCE_BYTES = 12

    def __init__(self, key: str | bytes, ig_account_id: str | UUID) -> None:
        key_bytes = self._validated_key(key)
        self._cipher = AESGCM(key_bytes)
        self._aad = f"engagemate:ig-account:{ig_account_id}".encode("utf-8")

    @classmethod
    def validate_key(cls, key: str | bytes) -> None:
        """Fail fast when runtime configuration is not a canonical AES-256 key."""

        cls._validated_key(key)

    @staticmethod
    def _validated_key(key: str | bytes) -> bytes:
        if isinstance(key, bytes):
            key_bytes = key
        else:
            try:
                key_bytes = _decode_base64url(key)
            except SessionCipherError as error:
                raise ValueError(
                    "session encryption key must be canonical base64url and decode "
                    "to exactly 32 bytes"
                ) from error
        if len(key_bytes) != 32:
            raise ValueError(
                "session encryption key must be canonical base64url and decode "
                "to exactly 32 bytes"
            )
        return key_bytes

    def __repr__(self) -> str:
        return "SessionCipher(version='v1', key=<redacted>, aad=<redacted>)"

    def encrypt(self, payload: Mapping[str, Any]) -> str:
        """Return a random-nonce, canonical-JSON session envelope."""

        try:
            plaintext = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise SessionCipherError("session payload is not valid JSON") from error

        nonce = os.urandom(self._NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, plaintext, self._aad)
        return ".".join(
            (self._VERSION, _encode_base64url(nonce), _encode_base64url(ciphertext))
        )

    def decrypt(self, token: str) -> dict[str, Any]:
        """Authenticate and decode an account-bound session envelope."""

        try:
            version, nonce_text, ciphertext_text = token.split(".")
        except (AttributeError, ValueError) as error:
            raise SessionCipherError("invalid session envelope") from error
        if version != self._VERSION:
            raise SessionCipherError("unsupported session envelope version")

        nonce = _decode_base64url(nonce_text)
        ciphertext = _decode_base64url(ciphertext_text)
        if len(nonce) != self._NONCE_BYTES:
            raise SessionCipherError("invalid session envelope")
        try:
            plaintext = self._cipher.decrypt(nonce, ciphertext, self._aad)
            payload = json.loads(plaintext)
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SessionCipherError("session envelope authentication failed") from error
        if not isinstance(payload, dict):
            raise SessionCipherError("session envelope payload must be an object")
        return payload
