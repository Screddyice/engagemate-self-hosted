"""Decode text columns a SQL_ASCII database hands back as bytes.

psycopg cannot decode text on a SQL_ASCII connection, so it returns `bytes`
and every comparison against a `str` silently fails. Neon is UTF8 and
production never sees this, but a developer whose local cluster was created
with the default encoding will, and the failure looks like a logic bug rather
than an encoding one.

One copy, because two drifted almost immediately.
"""

from __future__ import annotations


def text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
