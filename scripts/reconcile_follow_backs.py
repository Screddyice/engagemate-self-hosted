"""Audit all recorded follows through the bound session; never engage with targets.

Stream this file to the worker container: python - HANDLE [--record].
Pass --record to retain observations in the relationship evidence ledger.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

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


def record(pool, account, row, profile, observed_at):
    # Merge under a row lock; never overwrite follow/unfollow action receipts.
    with pool.connection() as c:
        current = c.execute(
            "SELECT evidence FROM relationship_follows WHERE id=%s AND "
            "user_id=%s AND ig_account_id=%s AND owner_id=%s FOR UPDATE",
            (row["id"], account["user_id"], account["id"], profile["owner_id"]),
        ).fetchone()
        if current is None:
            raise RuntimeError("relationship_changed")
        evidence = current["evidence"] or {}
        observation = {
            **profile,
            "observed_at": observed_at,
            "identity_matches": True,
            "source": "profile_reconciliation",
        }
        evidence["reconciliation"] = observation
        baseline = evidence.get("follow", {})
        if (
            baseline.get("outcome") == "confirmed"
            and baseline.get("followed_by") is False
            and profile["followed_by"] is True
        ):
            evidence.setdefault("first_follow_back", observation)
        c.execute(
            "UPDATE relationship_follows SET evidence=%s,updated_at=now() WHERE "
            "id=%s AND user_id=%s AND ig_account_id=%s",
            (Jsonb(evidence), row["id"], account["user_id"], account["id"]),
        )


def emit(data):
    print(json.dumps(data, default=str), flush=True)


async def main(args):
    settings = Settings()
    pool = create_pool(settings.database_url, min_size=1, max_size=3)
    try:
        with pool.connection() as c:
            accounts = c.execute(
                "SELECT id,user_id,ig_user_id FROM ig_accounts WHERE username=%s "
                "AND health_status='active' AND paused_at IS NULL",
                (args.handle.lstrip("@"),),
            ).fetchall()
            if len(accounts) != 1:
                raise RuntimeError("active_account_not_unique")
            account = accounts[0]
            targets = c.execute(
                "SELECT f.id,f.owner_id,t.username,f.confirmed_follow_at,f.evidence "
                "FROM relationship_follows f JOIN targets t ON t.id=f.target_id AND "
                "t.user_id=f.user_id AND t.ig_account_id=f.ig_account_id WHERE "
                "f.user_id=%s AND f.ig_account_id=%s AND f.confirmed_follow_at IS "
                "NOT NULL AND f.evidence #>> '{follow,outcome}'='confirmed' ORDER "
                "BY f.confirmed_follow_at",
                (account["user_id"], account["id"]),
            ).fetchall()
        emit(
            {
                "account": args.handle,
                "targets": len(targets),
                "instagram_read_only": True,
                "record_observations": args.record,
            }
        )
        if len(targets) > 30:
            raise RuntimeError("bounded_audit_limit")
        bound = await asyncio.to_thread(
            ProfileCaptureService(pool, settings)._load,
            account["user_id"],
            account["id"],
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
                    c["value"]
                    for c in await browser.context.cookies("https://www.instagram.com")
                    if c.get("name") == "ds_user_id"
                }
                if identities != {text(account["ig_user_id"])}:
                    raise RuntimeError("sender_identity_mismatch")
                for row in targets:
                    handle = text(row["username"])
                    owner = text(row["owner_id"])
                    page = await browser.context.new_page()

                    # Playwright passes the request as a second positional argument
                    # when a handler exposes one; keep this handler single-argument.
                    # Each page closes before the loop advances its scoped identity.
                    async def guard(route):
                        if _read_request(route.request, handle, owner):
                            await route.continue_()
                        else:
                            await route.abort()

                    async def block_socket(socket):
                        await socket.close()

                    await page.route("**/*", guard)
                    await page.route_web_socket("**/*", block_socket)
                    try:
                        profile = await PlaywrightFollowerBrowser(page).inspect(handle)
                        if profile["owner_id"] != owner:
                            raise RuntimeError("target_identity_mismatch")
                        if type(profile["followed_by"]) is not bool:
                            raise RuntimeError("relationship_unknown")
                        observed_at = datetime.now(UTC).isoformat()
                        if args.record:
                            record(pool, account, row, profile, observed_at)
                        emit(
                            {
                                "handle": handle,
                                "relationship_id": str(row["id"]),
                                "observed_at": observed_at,
                                "recorded": args.record,
                                "baseline_followed_by": row["evidence"]
                                .get("follow", {})
                                .get("followed_by"),
                                **profile,
                            }
                        )
                    except Exception as error:
                        emit(
                            {
                                "handle": handle,
                                "error": getattr(error, "reason", type(error).__name__),
                                "stopped": True,
                            }
                        )
                        return 1
                    finally:
                        await page.close()
                    await asyncio.sleep(3)
        emit({"complete": True, "checked": len(targets)})
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handle")
    parser.add_argument("--record", action="store_true")
    try:
        status = asyncio.run(main(parser.parse_args()))
    except Exception as error:
        emit({"stopped": True, "error": getattr(error, "reason", type(error).__name__)})
        status = 1
    raise SystemExit(status)
