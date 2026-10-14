"""Exercise terminal skips against the migrated local database."""
import pytest

from engagemate.db.executions import ClaimMetadata, ExecutionRepository
from engagemate.db.text import text
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401


@pytest.mark.parametrize("action", ["view", "like", "reply"])
async def test_skip_releases_reservation_and_cannot_be_replayed(world, action):
    repository = ExecutionRepository(world["pool"])
    metadata = ClaimMetadata(
        world["user_id"], world["account_id"], world["target_id"], "b" * 64,
        action, "That beach looks lovely" if action == "reply" else None,
        "engagemate-agent", "test unavailable story", 1.0,
    )
    claim = await repository.claim(metadata)
    assert not claim.duplicate
    assert await repository.mark_skipped(
        world["user_id"], world["account_id"], claim.engagement_id, "story_unavailable"
    )
    assert not await repository.mark_sent(
        world["user_id"], world["account_id"], claim.engagement_id
    )
    assert not await repository.mark_failed(
        world["user_id"], world["account_id"], claim.engagement_id, "action_failed"
    )
    repeated = await repository.claim(metadata)
    assert repeated.duplicate and repeated.engagement_id == claim.engagement_id
    with world["pool"].connection() as c:
        row = c.execute("SELECT status,failure_code,executed_at FROM story_engagements WHERE id=%s", (claim.engagement_id,)).fetchone()
        usage = c.execute("SELECT status,occurred_at FROM engagement_usage_events WHERE engagement_id=%s", (claim.engagement_id,)).fetchall()
    assert text(row["status"]) == "skipped"
    assert text(row["failure_code"]) == "story_unavailable" and row["executed_at"] is None
    assert len(usage) == (0 if action == "view" else 1)
    assert all(text(item["status"]) == "released" and item["occurred_at"] is None for item in usage)
