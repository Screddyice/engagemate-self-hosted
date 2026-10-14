"""Tenant-scoped story dedupe history in one batch query.

Discovery asks two questions per run: which of these candidate stories have we
already touched, and when did we last successfully engage each target. Both are
answered by a single round trip so a run cannot fan out one query per story.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from engagemate.worker.story_types import DiscoveryHistory, TargetEngagement

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_MAX_FINGERPRINTS = 1_000
_MAX_TARGETS = 1_000

_LOOKUP_SQL = """
WITH seen AS (
    SELECT DISTINCT story_fingerprint AS fingerprint,
           NULL::uuid AS target_id,
           NULL::timestamptz AS engaged_at
    FROM story_engagements
    WHERE user_id = %(user_id)s
      AND ig_account_id = %(account_id)s
      AND story_fingerprint = ANY(%(fingerprints)s)
),
engaged AS (
    SELECT NULL::text AS fingerprint,
           target_id,
           max(created_at) AS engaged_at
    FROM story_engagements
    WHERE user_id = %(user_id)s
      AND ig_account_id = %(account_id)s
      AND status = 'sent'
      AND target_id = ANY(%(target_ids)s)
    GROUP BY target_id
)
SELECT 'seen' AS kind, fingerprint, target_id, engaged_at FROM seen
UNION ALL
SELECT 'engaged' AS kind, fingerprint, target_id, engaged_at FROM engaged
"""


class PostgresStoryHistoryRepository:
    """Answer discovery's dedupe questions without leaving the tenant."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def __repr__(self) -> str:
        return "PostgresStoryHistoryRepository(pool=<configured>)"

    async def lookup(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        fingerprints: frozenset[str],
        target_ids: frozenset[UUID],
    ) -> DiscoveryHistory:
        """Return seen fingerprints and the latest engagement per target."""

        safe_fingerprints = _validated_fingerprints(fingerprints)
        safe_target_ids = _validated_target_ids(target_ids)
        if type(user_id) is not UUID or type(ig_account_id) is not UUID:
            raise ValueError("invalid history tenant")

        # Nothing to ask about: skip the round trip rather than query for none.
        if not safe_fingerprints and not safe_target_ids:
            return DiscoveryHistory(frozenset(), ())

        rows = await asyncio.to_thread(
            self._lookup_sync,
            user_id,
            ig_account_id,
            safe_fingerprints,
            safe_target_ids,
        )
        return _history_from_rows(rows, fingerprints, target_ids)

    def _lookup_sync(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        fingerprints: list[str],
        target_ids: list[UUID],
    ) -> list[dict[str, Any]]:
        with self._pool.connection() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    _LOOKUP_SQL,
                    {
                        "user_id": user_id,
                        "account_id": ig_account_id,
                        "fingerprints": fingerprints,
                        "target_ids": target_ids,
                    },
                )
                return cursor.fetchall()


def _history_from_rows(
    rows: list[dict[str, Any]],
    queried_fingerprints: frozenset[str],
    queried_target_ids: frozenset[UUID],
) -> DiscoveryHistory:
    """Build the value discovery expects, dropping anything it did not ask for."""

    seen: set[str] = set()
    engagements: list[TargetEngagement] = []
    for row in rows:
        # SQL_ASCII clusters return bytes for text columns, including this
        # discriminator, so decode before comparing.
        if _text(row["kind"]) == "seen":
            value = _text(row["fingerprint"])
            if value in queried_fingerprints:
                seen.add(value)
            continue
        target_id = row["target_id"]
        engaged_at = row["engaged_at"]
        if target_id in queried_target_ids and engaged_at is not None:
            engagements.append(TargetEngagement(target_id, engaged_at))
    engagements.sort(key=lambda engagement: engagement.target_id.hex)
    return DiscoveryHistory(frozenset(seen), tuple(engagements))


def _validated_fingerprints(value: object) -> list[str]:
    if type(value) is not frozenset:
        raise ValueError("fingerprints must be a frozenset")
    if len(value) > _MAX_FINGERPRINTS:
        raise ValueError("too many fingerprints in one lookup")
    if any(
        type(item) is not str or _FINGERPRINT.fullmatch(item) is None for item in value
    ):
        raise ValueError("invalid story fingerprint")
    return sorted(value)


def _validated_target_ids(value: object) -> list[UUID]:
    if type(value) is not frozenset:
        raise ValueError("target ids must be a frozenset")
    if len(value) > _MAX_TARGETS:
        raise ValueError("too many targets in one lookup")
    if any(type(item) is not UUID for item in value):
        raise ValueError("invalid target id")
    return sorted(value, key=lambda item: item.hex)


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
