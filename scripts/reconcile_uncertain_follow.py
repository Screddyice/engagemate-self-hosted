"""Verify one uncertain follow without replaying it; --record releases its held DM."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from engagemate.browser.context import BrowserContextFactory
from engagemate.browser.lease import LeasedBrowserFactory
from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.db.text import text
from engagemate.onboarding.profile_capture import ProfileCaptureService
from engagemate.worker.follower_browser import PlaywrightFollowerBrowser
from engagemate.worker.relationship_browser import _read_request
from playwright.async_api import async_playwright
from psycopg.types.json import Jsonb


def record_confirmation(c, uid, aid, rid, profile, observed_at):
    now = datetime.now(UTC)
    if (
        type(observed_at) is not datetime
        or observed_at.utcoffset() != timedelta(0)
        or not now - timedelta(minutes=5) <= observed_at <= now
        or type(profile) is not dict
        or profile.get("following") is not True
        or profile.get("private") is not False
    ):
        raise ValueError("fresh public-follow observation required")
    row = c.execute(
        """SELECT f.*,t.username AS target_handle,c.follow_reel_message_enabled,
                  c.follow_reel_message_enabled_at FROM relationship_follows f
        JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id
        JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id
        JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
        WHERE f.id=%s AND f.user_id=%s AND f.ig_account_id=%s AND f.status='uncertain'
          AND f.follow_attempted_at IS NOT NULL AND f.unfollow_attempted_at IS NULL
          AND a.health_status='active' AND a.paused_at IS NULL AND c.enabled AND t.enabled
          AND NOT EXISTS (SELECT 1 FROM instagram_execution_controls x WHERE x.blocked AND
              (x.scope='system' OR (x.scope='action' AND x.action IN ('follow','dm'))
               OR (x.scope='tenant' AND x.user_id=f.user_id)
               OR (x.scope='account' AND x.user_id=f.user_id AND x.ig_account_id=f.ig_account_id)))
        FOR UPDATE OF f FOR SHARE OF c,t""",
        (rid, uid, aid),
    ).fetchone()
    if (
        not row
        or text(row["owner_id"]) != profile.get("owner_id")
        or text(row["target_handle"]) != profile.get("handle")
    ):
        raise ValueError("uncertain follow identity changed")
    baseline = (row["evidence"] or {}).get("follow", {})
    if not all(
        baseline.get(k) is True
        for k in ("identity_matches", "request_forwarded", "mutation_attempted")
    ) or not all(
        baseline.get(k) is False for k in ("following", "private", "followed_by")
    ):
        raise ValueError("original owned follow proof missing")
    observation = {
        "source": "operator_profile_reconciliation",
        "identity_matches": True,
        "following": True,
        "private": False,
        "observed_at": observed_at.isoformat(),
    }
    c.execute(
        """UPDATE relationship_follows SET status='following',confirmed_follow_at=follow_attempted_at,
        unfollow_due_at=follow_attempted_at+interval '120 hours',last_error=NULL,
        evidence=evidence || %s,updated_at=%s WHERE id=%s AND user_id=%s AND ig_account_id=%s""",
        (Jsonb({"reconciliation": observation}), now, rid, uid, aid),
    )
    message = c.execute(
        """SELECT id FROM follow_reel_messages WHERE relationship_follow_id=%s
        AND user_id=%s AND ig_account_id=%s AND status='uncertain' AND attempted_at IS NULL
        AND reserved_at >= %s AND reserved_at <= %s FOR UPDATE""",
        (rid, uid, aid, row["follow_reel_message_enabled_at"] or now, now),
    ).fetchone()
    resumed = False
    if message and row["follow_reel_message_enabled"]:
        slot = c.execute(
            """INSERT INTO follow_reel_rotation (user_id,ig_account_id,next_slot) VALUES (%s,%s,1)
            ON CONFLICT (user_id,ig_account_id) DO UPDATE SET next_slot=follow_reel_rotation.next_slot+1
            RETURNING next_slot-1 AS slot""",
            (uid, aid),
        ).fetchone()["slot"]
        c.execute(
            """UPDATE follow_reel_messages SET status='pending',rotation_slot=%s,
            evidence=evidence || %s,updated_at=%s WHERE id=%s""",
            (
                slot,
                Jsonb({"reason": "follow_reconciled_before_message_attempt"}),
                now,
                message["id"],
            ),
        )
        resumed = True
    c.execute(
        """INSERT INTO mcp_audit_events (user_id,ig_account_id,client_id,tool_name,outcome,before_state,after_state)
        VALUES (%s,%s,'operator','operator_uncertain_follow_reconciliation','updated',%s,%s)""",
        (
            uid,
            aid,
            Jsonb({"relationship_id": str(rid), "status": "uncertain"}),
            Jsonb(
                {
                    "relationship_id": str(rid),
                    "status": "following",
                    "observation": observation,
                    "unattempted_message_resumed": resumed,
                }
            ),
        ),
    )
    return resumed


async def main(args):
    settings = Settings()
    pool = create_pool(settings.database_url, min_size=1, max_size=2)
    uid, aid, rid = args.user_id, args.account_id, args.relationship_id
    try:
        with pool.connection() as c:
            c.execute("SET TRANSACTION READ ONLY")
            row = c.execute(
                "SELECT a.username AS sender,a.ig_user_id AS sender_id,f.owner_id,t.username AS target FROM relationship_follows f JOIN ig_accounts a ON a.id=f.ig_account_id AND a.user_id=f.user_id JOIN targets t ON t.id=f.target_id AND t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id WHERE f.id=%s AND f.user_id=%s AND f.ig_account_id=%s AND f.status='uncertain' AND a.health_status='active' AND a.paused_at IS NULL",
                (rid, uid, aid),
            ).fetchone()
        if not row:
            raise ValueError("active owned uncertain follow not found")
        handle, owner, sender = (
            text(row["target"]),
            text(row["owner_id"]),
            text(row["sender_id"]),
        )
        bound = await asyncio.to_thread(
            ProfileCaptureService(pool, settings)._load, uid, aid
        )
        async with async_playwright() as driver:

            class Chromium:
                async def launch_persistent_context(self, **kwargs):
                    kwargs["service_workers"] = "block"
                    return await driver.chromium.launch_persistent_context(**kwargs)

            factory = LeasedBrowserFactory(
                pool,
                BrowserContextFactory(playwright=SimpleNamespace(chromium=Chromium())),
            )
            async with factory.open(bound) as browser:
                identities = {
                    cookie["value"]
                    for cookie in await browser.context.cookies(
                        "https://www.instagram.com"
                    )
                    if cookie.get("name") == "ds_user_id"
                }
                if identities != {sender}:
                    raise ValueError("bound sender identity unavailable")
                page = await browser.context.new_page()

                async def guard(route):
                    if _read_request(route.request, handle, owner):
                        await route.continue_()
                    else:
                        await route.abort()

                async def block_socket(socket):
                    await socket.close()

                await page.route("**/*", guard)
                await page.route_web_socket("**/*", block_socket)
                profile = await PlaywrightFollowerBrowser(page).inspect(handle)
                if profile["owner_id"] != owner or profile["handle"] != handle:
                    raise ValueError("fresh target identity changed")
                resumed = False
                if args.record:
                    with pool.connection() as c, c.transaction():
                        resumed = record_confirmation(
                            c, uid, aid, rid, profile, datetime.now(UTC)
                        )
                print(
                    json.dumps(
                        {
                            "relationship_id": str(rid),
                            "instagram_read_only": True,
                            "recorded": args.record,
                            "unattempted_message_resumed": resumed,
                            "sender_identity_verified": True,
                            "target_identity_verified": True,
                            "following": profile.get("following"),
                            "private": profile.get("private"),
                            "followed_by": profile.get("followed_by"),
                        },
                        default=str,
                    ),
                    flush=True,
                )
                await page.close()
    finally:
        pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--account-id", type=UUID, required=True)
    parser.add_argument("--relationship-id", type=UUID, required=True)
    parser.add_argument("--record", action="store_true")
    asyncio.run(main(parser.parse_args()))
