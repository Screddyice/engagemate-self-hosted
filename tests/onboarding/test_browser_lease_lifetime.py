"""Worker exclusion outlives the server idle default without leaking pool state."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from engagemate.browser.lease import BrowserBusy, LeasedBrowserFactory, browser_lease
from tests.onboarding.test_profile_capture import database_url, world


@pytest.mark.parametrize('exit_mode', ['normal', 'error', 'cancel'])
async def test_worker_lease_survives_server_idle_timeout_and_releases(world, database_url, exit_mode):
    pool = ConnectionPool(database_url, min_size=1, max_size=1,
        kwargs={'row_factory': dict_row, 'options': '-c idle_in_transaction_session_timeout=100ms'})
    pool.wait()
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    original_pid = None

    class Factory:
        @asynccontextmanager
        async def open(self, context):
            entered.set()
            try:
                yield object()
            finally:
                closed.append(True)

    async def run():
        async with LeasedBrowserFactory(pool, Factory()).open(SimpleNamespace(ig_account_id=world.account)):
            await release.wait()
            if exit_mode == 'error':
                raise RuntimeError('test browser failure')

    try:
        with pool.connection() as conn:
            original_pid = conn.info.backend_pid
        task = asyncio.create_task(run())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            # Exceeds the 100ms server default while performing no lease queries.
            await asyncio.sleep(0.35)
            with pytest.raises(BrowserBusy), browser_lease(world.pool, world.account):
                pytest.fail('worker advisory lock expired during browser I/O')
            if exit_mode == 'cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                if exit_mode == 'error':
                    with pytest.raises(RuntimeError, match='test browser failure'):
                        await task
                else:
                    await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert closed == [True]
        with pool.connection() as conn:
            assert conn.info.backend_pid == original_pid
            assert conn.execute('SHOW idle_in_transaction_session_timeout').fetchone()['idle_in_transaction_session_timeout'] == '100ms'
        with browser_lease(world.pool, world.account):
            pass
    finally:
        pool.close()
