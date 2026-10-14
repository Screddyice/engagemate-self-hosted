from __future__ import annotations

import base64
import logging
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest


API_DIR = Path(__file__).resolve().parents[2] / "api"
sys.path.insert(0, str(API_DIR))

from engagemate.security.session_cipher import SessionCipher, SessionCipherError


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def test_round_trip_uses_versioned_canonical_envelope() -> None:
    account_id = uuid4()
    cipher = SessionCipher(_key(), account_id)
    payload = {
        "cookies": [{"value": "super-secret", "name": "sessionid"}],
        "user_agent": "Mozilla/5.0",
    }

    token = cipher.encrypt(payload)

    assert token.startswith("v1.")
    assert len(token.split(".")) == 3
    assert cipher.decrypt(token) == payload
    assert "super-secret" not in token


@pytest.mark.parametrize("part", [1, 2])
def test_tampered_envelope_is_rejected_without_leaking_plaintext(part: int) -> None:
    cipher = SessionCipher(_key(), uuid4())
    token = cipher.encrypt({"cookies": [{"value": "never-print-me"}]})
    segments = token.split(".")
    segments[part] = segments[part][:-1] + ("A" if segments[part][-1] != "A" else "B")

    with pytest.raises(SessionCipherError) as error:
        cipher.decrypt(".".join(segments))

    assert "never-print-me" not in str(error.value)


def test_wrong_key_unknown_version_and_aad_mismatch_are_rejected() -> None:
    account_id = uuid4()
    token = SessionCipher(_key(), account_id).encrypt(
        {"cookies": [{"value": "do-not-leak"}]}
    )

    with pytest.raises(SessionCipherError):
        SessionCipher(_key(), account_id).decrypt(token)
    with pytest.raises(SessionCipherError):
        SessionCipher(_key(), uuid4()).decrypt(token)
    with pytest.raises(SessionCipherError, match="unsupported session envelope version"):
        SessionCipher(_key(), account_id).decrypt(token.replace("v1.", "v2.", 1))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: token.replace(".", ".=", 1),
        lambda token: token.replace(".", ".$", 1),
        lambda token: token + "=",
        lambda token: token.replace(".", "..", 1),
    ],
)
def test_envelope_rejects_noncanonical_base64url(mutate) -> None:
    cipher = SessionCipher(_key(), uuid4())
    token = cipher.encrypt({"cookies": [{"value": "hidden"}]})

    with pytest.raises(SessionCipherError, match="invalid session envelope"):
        cipher.decrypt(mutate(token))


def test_secret_material_is_absent_from_repr_exceptions_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cookie = "cookie-value-that-must-never-appear"
    key = _key()
    cipher = SessionCipher(key, uuid4())

    with caplog.at_level(logging.DEBUG):
        token = cipher.encrypt({"cookies": [{"name": "sessionid", "value": cookie}]})
        with pytest.raises(SessionCipherError) as error:
            cipher.decrypt(token[:-2] + "xx")

    combined = repr(cipher) + str(error.value) + caplog.text
    assert cookie not in combined
    assert key not in combined


@pytest.mark.parametrize(
    "key",
    [
        "",
        "short",
        base64.urlsafe_b64encode(b"x" * 31).decode(),
        base64.urlsafe_b64encode(b"x" * 32).decode(),
        _key() + "$",
    ],
)
def test_key_must_decode_to_exactly_32_bytes(key: str) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        SessionCipher(key, uuid4())
