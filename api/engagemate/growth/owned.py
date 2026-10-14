"""Daily growth observations through each owner's existing leased Instagram session."""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import logging
from types import SimpleNamespace
from uuid import UUID

from playwright.async_api import async_playwright

from engagemate.browser.context import BrowserContextFactory
from engagemate.browser.lease import browser_lease
from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.db.text import text
from engagemate.growth.metrics import parse_profile_metrics
from engagemate.growth.repository import GrowthSnapshotRepository
from engagemate.onboarding.profile_capture import ProfileCaptureService, ProfileCaptureError
from engagemate.worker.follower_browser import _nodes, _identity, _public_count, _check_payload, FollowerBrowserError, PlaywrightFollowerBrowser
from engagemate.worker.relationship_browser import _read_request
from engagemate.worker.story_actions import _form


def owned_profile(payload, handle, owner):
    _check_payload(payload)
    matches=[]
    for node in _nodes(payload):
        if node.get('username') != handle or _identity(node) != owner:
            continue
        if not all(k in node for k in ('follower_count','following_count','media_count')):
            continue
        matches.append({
            'followersCount':_public_count(node['follower_count']),
            'followingCount':_public_count(node['following_count']),
            'postsCount':_public_count(node['media_count']),
        })
    if not matches or any(row != matches[0] for row in matches[1:]):
        raise FollowerBrowserError('profile_unreadable')
    return matches[0]


def owned_posts(payload, owner):
    _check_payload(payload)
    posts={}
    for node in _nodes(payload):
        user=node.get('user')
        if not isinstance(user,dict) or _identity(user) != owner or node.get('media_type') not in (1,2,8):
            continue
        key=node.get('pk')
        if type(key) not in (int,str) or not str(key).isdecimal():
            continue
        try:
            row={'likesCount':_public_count(node.get('like_count')),
                 'commentsCount':_public_count(node.get('comment_count')),
                 'timestamp':_public_count(node.get('taken_at'))}
        except FollowerBrowserError:
            continue  # Hidden engagement is unknown, not zero.
        if str(key) in posts and posts[str(key)] != row:
            raise FollowerBrowserError('profile_ambiguous')
        posts[str(key)]=row
    return sorted(posts.values(),key=lambda p:p['timestamp'],reverse=True)[:12]


def parse_growth_query(kind, status, payload, handle, owner):
    """Keep an optional post sample from invalidating a verified own profile."""
    try:
        PlaywrightFollowerBrowser._check_status(status, payload)
    except FollowerBrowserError as error:
        if error.reason != 'response_failed' or kind != 'PolarisProfilePostsQuery':
            raise
        logging.info('owned_growth posts_status=unavailable reason=response_failed')
        return None
    if status != 200:
        if kind == 'PolarisProfilePostsQuery':
            logging.info('owned_growth posts_status=unavailable reason=response_failed')
            return None
        raise FollowerBrowserError('response_failed')
    if kind == 'PolarisProfilePageContentQuery':
        return owned_profile(payload, handle, owner)
    if kind != 'PolarisProfilePostsQuery':
        raise ValueError('unknown growth query')
    try:
        return owned_posts(payload, owner)
    except FollowerBrowserError as error:
        if error.reason in {'rate_limited', 'authentication_required', 'checkpoint_required'}:
            raise
        logging.info('owned_growth posts_status=unavailable reason=%s', error.reason)
        return None


async def capture(pool, settings, account):
    with pool.connection() as c:
        row=c.execute('''SELECT username,ig_user_id FROM ig_accounts
            WHERE id=%s AND user_id=%s AND health_status='active' AND paused_at IS NULL''',
            (account.ig_account_id,account.user_id)).fetchone()
    if row is None:
        raise ProfileCaptureError('reconnect_required')
    handle,owner=text(row['username']),text(row['ig_user_id'])
    if handle != account.username or not owner or not owner.isdecimal():
        raise ProfileCaptureError('sender_identity_mismatch')
    service=ProfileCaptureService(pool,settings)
    with browser_lease(pool,account.ig_account_id,profile_capture=True):
        await asyncio.to_thread(service._reserve,account.user_id,account.ig_account_id)
        bound=await asyncio.to_thread(service._load,account.user_id,account.ig_account_id)
        try:
            async with asyncio.timeout(60), async_playwright() as pw:
                class Chromium:
                    async def launch_persistent_context(self,**kwargs):
                        kwargs['service_workers']='block'
                        return await pw.chromium.launch_persistent_context(**kwargs)
                async with BrowserContextFactory(playwright=SimpleNamespace(chromium=Chromium())).open(bound) as browser:
                    cookies=await browser.context.cookies('https://www.instagram.com')
                    identities={c['value'] for c in cookies if c['name']=='ds_user_id'}
                    del cookies
                    if identities != {owner}:
                        raise ProfileCaptureError('sender_identity_mismatch')
                    page=await browser.context.new_page()
                    tasks=set();profile_ready=asyncio.Event();posts_ready=asyncio.Event()
                    result={};failure=[]
                    async def guard(route):
                        try:
                            allowed=_read_request(route.request,handle,owner)
                        except Exception:
                            allowed=False
                        try:
                            if allowed and not failure:
                                await route.continue_()
                            else:
                                await route.abort()
                        except Exception:
                            pass  # Closed page cannot forward writes.
                    async def block_socket(socket):
                        await socket.close()
                    async def consume(response):
                        try:
                            form=_form(response.request) if response.request.method=='POST' else None
                            if not form or form[0] not in ('PolarisProfilePageContentQuery','PolarisProfilePostsQuery'):
                                return
                            payload=await response.json() if response.status == 200 else None
                            parsed=parse_growth_query(form[0],response.status,payload,handle,owner)
                            if form[0]=='PolarisProfilePageContentQuery':
                                result.update(parsed);profile_ready.set()
                            else:
                                if parsed is not None:
                                    result['latestPosts']=parsed
                                posts_ready.set()
                        except FollowerBrowserError as error:
                            failure.append(error.reason);profile_ready.set();posts_ready.set()
                        except Exception:
                            if form and form[0]=='PolarisProfilePostsQuery':
                                logging.info('owned_growth posts_status=unavailable reason=response_unreadable')
                                posts_ready.set()
                            else:
                                failure.append('profile_unreadable');profile_ready.set();posts_ready.set()
                    def observe(response):
                        task=asyncio.create_task(consume(response));tasks.add(task);task.add_done_callback(tasks.discard)
                    await page.route('**/*',guard)
                    await page.route_web_socket('**/*',block_socket)
                    cdp=await browser.context.new_cdp_session(page)
                    await cdp.send('Network.setBypassServiceWorker',{'bypass':True})
                    page.on('response',observe)
                    try:
                        await page.goto(f'https://www.instagram.com/{handle}/',wait_until='domcontentloaded',timeout=30000)
                        await asyncio.wait_for(profile_ready.wait(),25)
                        if not failure:
                            try:
                                await asyncio.wait_for(posts_ready.wait(),10)
                            except TimeoutError:
                                pass  # Valid follower count, unknown engagement sample.
                        if failure:
                            raise FollowerBrowserError(failure[0])
                        return parse_profile_metrics(result)
                    finally:
                        page.remove_listener('response',observe)
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks,return_exceptions=True)
        except FollowerBrowserError as error:
            restricted=error.reason in {'rate_limited','authentication_required','checkpoint_required'}
            await asyncio.to_thread(service._failure,account.user_id,account.ig_account_id,error.reason,restricted)
            raise ProfileCaptureError(error.reason) from None
        except TimeoutError:
            await asyncio.to_thread(service._failure,account.user_id,account.ig_account_id,'profile_check_timeout',False)
            raise ProfileCaptureError('profile_check_timeout') from None


async def execute(user_id=None, dry_run=False):
    settings=Settings()
    failures=0
    with create_pool(settings.database_url,min_size=1,max_size=4) as pool:
        repository=GrowthSnapshotRepository(pool,source='account_session')
        accounts=repository.list_due(datetime.now(UTC),settings.growth_snapshot_batch_size,user_id=user_id)
        for account in accounts:
            if dry_run:
                logging.info('owned_growth status=due account=%s',account.ig_account_id)
                continue
            try:
                metrics=await capture(pool,settings,account)
                if metrics.status=='unavailable':
                    raise ProfileCaptureError('profile_unreadable')
                accepted=repository.upsert(account,metrics,datetime.now(UTC))
                logging.info('owned_growth status=%s accepted=%s account=%s',metrics.status,accepted,account.ig_account_id)
            except Exception:
                failures+=1
                logging.error('owned_growth status=unavailable account=%s',account.ig_account_id)
    return 1 if failures else 0


def main():
    logging.basicConfig(level=logging.INFO)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',type=UUID)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    return asyncio.run(execute(args.user_id,args.dry_run))


if __name__=='__main__':
    raise SystemExit(main())
