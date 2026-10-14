"""An env file that a shell cannot read is a deploy-time trap.

deploy.sh, engagemate-container-watchdog.service, and the documented
healthcheck procedure all read deploy/.env.* with `set -a; . <file>`. Two
production values did not survive that, and both failed as
"configuration error", which points at the config rather than the reader.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

_spec = importlib.util.spec_from_file_location(
    "engagemate_healthcheck", DEPLOY / "healthcheck.py"
)
assert _spec is not None and _spec.loader is not None
healthcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(healthcheck)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # The two that actually broke production tooling.
        ("DATABASE_URL=postgresql://u:p@h/db?a=1&sslmode=require", ["DATABASE_URL"]),
        ('FINGERPRINT_GEO_MAP_JSON=[{"country":"US"}]', ["FINGERPRINT_GEO_MAP_JSON"]),
        # Quoting is the fix, in either style.
        ("DATABASE_URL='postgresql://u:p@h/db?a=1&sslmode=require'", []),
        ('FINGERPRINT_GEO_MAP_JSON=\'[{"country":"US"}]\'', []),
        # Ordinary bare values stay bare.
        ("WORKER_ID=engagemate-worker-1", []),
        ("STRIPE_SECRET_KEY=sk_live_abc123", []),
        ("CONTROL_API_URL=http://api:8000", []),
        ("IPROYAL_MODE=inventory", []),
        # Empty values and comments are not assignments worth flagging.
        ("DATABASE_URL=", []),
        ("# DATABASE_URL=postgresql://u:p@h/db?a=1&b=2", []),
        ("", []),
    ],
)
def test_flags_exactly_the_values_a_shell_would_mangle(
    line: str, expected: list[str]
) -> None:
    assert healthcheck.unsafe_env_assignments(line) == expected


def test_a_shell_really_does_lose_an_unquoted_ampersand_value() -> None:
    """Pin the actual bash behaviour, so the rule is not folklore."""

    unquoted = "DATABASE_URL=postgresql://h/db?a=1&sslmode=require"
    result = subprocess.run(
        ["bash", "-c", f"set -a; eval {unquoted!r} 2>/dev/null; set +a; echo \"[${{DATABASE_URL:-}}]\""],
        capture_output=True,
        text=True,
        check=False,
    )
    # bash treats `NAME=a&b` as a backgrounded assignment: the parent never
    # sees NAME. Not truncated, absent.
    assert result.stdout.strip() == "[]"


def test_quoting_makes_the_same_value_survive_a_shell() -> None:
    quoted = "DATABASE_URL='postgresql://h/db?a=1&sslmode=require'"
    result = subprocess.run(
        ["bash", "-c", f"set -a; eval {quoted!r}; set +a; echo \"${{DATABASE_URL}}\""],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "postgresql://h/db?a=1&sslmode=require"


def test_validator_names_every_offending_key(tmp_path: Path) -> None:
    env = tmp_path / ".env.api"
    env.write_text(
        "WORKER_ID=fine\n"
        "DATABASE_URL=postgresql://h/db?a=1&b=2\n"
        'FINGERPRINT_GEO_MAP_JSON=[{"country":"US"}]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError) as error:
        healthcheck.validate_env_file_is_shell_safe(str(env))
    message = str(error.value)
    assert "DATABASE_URL" in message
    assert "FINGERPRINT_GEO_MAP_JSON" in message
    assert "WORKER_ID" not in message


def test_validator_accepts_a_quoted_file(tmp_path: Path) -> None:
    env = tmp_path / ".env.api"
    env.write_text(
        "WORKER_ID=fine\n"
        "DATABASE_URL='postgresql://h/db?a=1&b=2'\n",
        encoding="utf-8",
    )
    healthcheck.validate_env_file_is_shell_safe(str(env))


def test_shipped_example_files_are_shell_safe() -> None:
    examples = sorted(DEPLOY.glob(".env.*.example"))
    assert examples, "expected shipped .env.*.example files"
    for example in examples:
        healthcheck.validate_env_file_is_shell_safe(str(example))


def test_cli_exposes_the_check() -> None:
    result = subprocess.run(
        [sys.executable, str(DEPLOY / "healthcheck.py"), "env-file", "/nonexistent"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "cannot read" in result.stderr
