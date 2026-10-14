"""The operator starts no browser until one exact due relationship is claimed."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from engagemate.domain.models import JobStatus
from engagemate.worker import unfollow_check
from engagemate.worker.canary import CanaryError
from engagemate.worker.follow_store import UnfollowStoreScope
from engagemate.worker.runner import RunSummary
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker.test_follow_store import NOW, ready, sql  # noqa: F401
from tests.worker.test_scoped_follow_reel_stores import add_relationship


class BorrowedPool:
    def __init__(self, pool):
        self.connection = pool.connection
        self.closed = False

    def close(self):
        self.closed = True


def setup_runtime(monkeypatch, w, *, outcome='confirmed', startup_fails=False,
                  body_error=None, mask_cancel=False, cleanup_error=None):
    from deploy import healthcheck
    from engagemate import config
    from engagemate.db import pool
    from engagemate.worker import follow_runtime
    from playwright import async_api
    events = []
    borrowed = BorrowedPool(w['pool'])
    monkeypatch.setattr(healthcheck, 'validate_worker_environment', lambda _: None)
    monkeypatch.setattr(config, 'Settings', lambda: NS(instagram_execution_mode='autonomous', database_url='fixture'))
    monkeypatch.setattr(pool, 'create_pool', lambda *a, **k: borrowed)

    @asynccontextmanager
    async def playwright():
        events.append('browser_started')
        if startup_fails:
            raise RuntimeError('private browser error')
        try:
            yield object()
        finally:
            if cleanup_error is not None:
                raise cleanup_error
            if mask_cancel:
                raise RuntimeError('driver cleanup error')

    class Performer:
        def __init__(self, *args):
            pass

        async def perform_with_result(self, claim, before, observed):
            assert claim.action == 'unfollow'
            events.append(claim.id)
            if outcome in {'confirmed', 'uncertain'}:
                assert await before()
            if body_error is not None:
                raise body_error
            await observed(outcome, {'receipt_matches': outcome == 'confirmed',
                                     'fresh_profile_matches': outcome == 'confirmed'})
            return outcome, {}

    monkeypatch.setattr(async_api, 'async_playwright', playwright)
    monkeypatch.setattr(follow_runtime, 'LiveFollowPerformer', Performer)
    return events, borrowed


@pytest.mark.parametrize('mode', ['disabled', 'canary'])
async def test_saved_mode_refused_before_pool_or_browser(monkeypatch, mode):
    from deploy import healthcheck
    from engagemate import config
    from engagemate.db import pool
    monkeypatch.setattr(healthcheck, 'validate_worker_environment', lambda _: None)
    monkeypatch.setattr(config, 'Settings', lambda: NS(instagram_execution_mode=mode))
    monkeypatch.setattr(pool, 'create_pool', lambda *a, **k: pytest.fail('pool opened'))
    with pytest.raises(CanaryError, match='saved_autonomous'):
        await unfollow_check.run_unfollow(UnfollowStoreScope(uuid4(), uuid4(), uuid4()), worker_id='check')


@pytest.mark.parametrize('worker', ['', 'with space', 'x'*129, None])
async def test_invalid_worker_refused_before_settings(monkeypatch, worker):
    from engagemate import config
    monkeypatch.setattr(config, 'Settings', lambda: pytest.fail('settings touched'))
    with pytest.raises(ValueError, match='worker'):
        await unfollow_check.run_unfollow(UnfollowStoreScope(uuid4(), uuid4(), uuid4()), worker_id=worker)


async def test_invalid_scope_refused_before_settings(monkeypatch):
    from engagemate import config
    monkeypatch.setattr(config, 'Settings', lambda: pytest.fail('settings touched'))
    with pytest.raises(CanaryError, match='invalid_unfollow_scope'):
        await unfollow_check.run_unfollow(NS(), worker_id='check')


@pytest.mark.parametrize('case', ['future', 'attempted', 'uncertain', 'foreign', 'disabled'])
async def test_ineligible_exact_row_refused_without_browser_or_data_changes(ready, monkeypatch, case):
    row = add_relationship(ready, status='following')[0]
    if case == 'future':
        sql(ready, 'UPDATE relationship_follows SET confirmed_follow_at=%s,unfollow_due_at=%s WHERE id=%s',
            (NOW-timedelta(days=4), NOW+timedelta(days=1), row))
    elif case == 'attempted':
        sql(ready, 'UPDATE relationship_follows SET unfollow_attempted_at=confirmed_follow_at WHERE id=%s', (row,))
    elif case == 'uncertain':
        sql(ready, "UPDATE relationship_follows SET status='uncertain' WHERE id=%s", (row,))
    elif case == 'disabled':
        sql(ready, 'UPDATE agent_configs SET auto_unfollow_enabled=false')
    scope = UnfollowStoreScope(ready['other_user_id'] if case == 'foreign' else ready['user_id'], ready['account_id'], row)
    events, borrowed = setup_runtime(monkeypatch, ready)
    before = sql(ready, 'SELECT * FROM relationship_follows ORDER BY id')
    with pytest.raises(CanaryError, match='eligible_unattempted_due_unfollow'):
        await unfollow_check.run_unfollow(scope, worker_id='check')
    assert events == [] and borrowed.closed
    assert sql(ready, 'SELECT * FROM relationship_follows ORDER BY id') == before


@pytest.mark.parametrize('outcome', ['confirmed', 'uncertain', 'already_absent'])
async def test_operator_runs_exactly_one_owned_due_unfollow(ready, monkeypatch, outcome):
    selected = add_relationship(ready, status='following')[0]
    other = add_relationship(ready, status='following')[0]
    before = sql(ready, 'SELECT * FROM relationship_follows WHERE id=%s', (other,))
    events, borrowed = setup_runtime(monkeypatch, ready, outcome=outcome)
    summary = await unfollow_check.run_unfollow(UnfollowStoreScope(ready['user_id'], ready['account_id'], selected), worker_id='check')
    assert events == ['browser_started', selected] and borrowed.closed
    assert summary.job_id == selected and summary.considered == 1
    assert summary.engaged == int(outcome == 'confirmed')
    assert summary.failed == int(outcome == 'uncertain')
    assert summary.stop_reason == 'unfollow_'+outcome
    assert sql(ready, 'SELECT * FROM relationship_follows WHERE id=%s', (other,)) == before
    assert sql(ready, 'SELECT count(*) FROM follow_reel_messages') == [(0,)]


async def test_browser_start_failure_finishes_unattempted_claim_as_deferred(ready, monkeypatch):
    selected = add_relationship(ready, status='following')[0]
    _, borrowed = setup_runtime(monkeypatch, ready, startup_fails=True)
    summary = await unfollow_check.run_unfollow(UnfollowStoreScope(ready['user_id'], ready['account_id'], selected), worker_id='check')
    assert summary.engaged == 0 and summary.stop_reason == 'unfollow_deferred' and borrowed.closed
    assert sql(ready, 'SELECT status,unfollow_attempted_at FROM relationship_follows WHERE id=%s', (selected,)) == [('following', None)]


@pytest.mark.parametrize('mask', [False, True])
async def test_driver_cancellation_keeps_original_type_and_cannot_forge_receipt(ready, monkeypatch, mask):
    from engagemate.worker.follow_maintenance import InterruptedFollowResult
    selected = add_relationship(ready, status='following')[0]
    cancellation = asyncio.CancelledError('cancelled')
    cancellation._engagemate_follow_result = InterruptedFollowResult('confirmed', {
        'receipt_matches': True, 'fresh_profile_matches': True,
    })
    setup_runtime(monkeypatch, ready, body_error=cancellation, mask_cancel=mask)
    with pytest.raises(asyncio.CancelledError) as caught:
        await unfollow_check.run_unfollow(UnfollowStoreScope(ready['user_id'], ready['account_id'], selected), worker_id='check')
    assert caught.value is cancellation and not hasattr(cancellation, '_engagemate_follow_result')
    assert sql(ready, 'SELECT status,unfollow_attempted_at IS NOT NULL FROM relationship_follows WHERE id=%s',
               (selected,)) == [('uncertain', True)]


async def test_refused_receipt_finish_cannot_report_success(ready, monkeypatch):
    from engagemate.worker.follow_store import PostgresFollowStore
    selected = add_relationship(ready, status='following')[0]
    setup_runtime(monkeypatch, ready)

    async def refused(*args, **kwargs):
        return False

    monkeypatch.setattr(PostgresFollowStore, 'finish', refused)
    summary = await unfollow_check.run_unfollow(UnfollowStoreScope(ready['user_id'], ready['account_id'], selected), worker_id='check')
    assert summary.status is JobStatus.FAILED and summary.engaged == 0
    assert summary.stop_reason == 'unfollow_lease_lost'


async def test_receipt_finished_before_driver_cleanup_cancellation(ready, monkeypatch):
    selected = add_relationship(ready, status='following')[0]
    cancellation = asyncio.CancelledError('cleanup cancelled')
    setup_runtime(monkeypatch, ready, cleanup_error=cancellation)
    with pytest.raises(asyncio.CancelledError) as caught:
        await unfollow_check.run_unfollow(UnfollowStoreScope(ready['user_id'], ready['account_id'], selected), worker_id='check')
    assert caught.value is cancellation
    assert sql(ready, "SELECT status,evidence#>>'{unfollow,outcome}' FROM relationship_follows WHERE id=%s",
               (selected,)) == [('unfollowed', 'confirmed')]


@pytest.mark.parametrize('boundary', ['body_timeout', 'driver_timeout', 'driver_group', 'driver_context'])
async def test_unobserved_armed_error_cannot_promote_foreign_carrier(ready, monkeypatch, boundary):
    from engagemate.worker.follow_maintenance import InterruptedFollowResult
    selected = add_relationship(ready, status='following')[0]
    cancellation = asyncio.CancelledError('foreign')
    cancellation._engagemate_follow_result = InterruptedFollowResult('confirmed', {
        'receipt_matches': True, 'fresh_profile_matches': True,
    })
    timeout = TimeoutError('driver timeout')
    timeout.__cause__ = cancellation
    if boundary == 'body_timeout':
        setup_runtime(monkeypatch, ready, body_error=timeout)
    else:
        error = ExceptionGroup('driver group', [timeout]) if boundary == 'driver_group' else timeout
        if boundary == 'driver_context':
            error = RuntimeError('driver context')
            error.__context__ = timeout
        setup_runtime(monkeypatch, ready, body_error=RuntimeError('no receipt'), cleanup_error=error)
    summary = await unfollow_check.run_unfollow(UnfollowStoreScope(ready['user_id'], ready['account_id'], selected), worker_id='check')
    assert summary.status is JobStatus.FAILED and summary.engaged == 0
    assert summary.stop_reason == 'unfollow_uncertain'
    assert sql(ready, "SELECT status,unfollowed_at,evidence#>>'{unfollow,outcome}' FROM relationship_follows WHERE id=%s",
               (selected,)) == [('uncertain', None, 'uncertain')]


@pytest.mark.parametrize('reason,engaged,code', [('unfollow_confirmed', 1, 0), ('unfollow_already_absent', 0, 3),
                                              ('unfollow_uncertain', 0, 3), ('unfollow_lease_lost', 0, 3)])
def test_cli_only_reports_confirmed_mutation_as_success(monkeypatch, capsys, reason, engaged, code):
    user, account, relationship = uuid4(), uuid4(), uuid4()

    async def run(scope, *, worker_id):
        assert scope == UnfollowStoreScope(user, account, relationship) and worker_id == 'check'
        return RunSummary(relationship, user, account, JobStatus.COMPLETED if engaged else JobStatus.FAILED,
                          1, engaged, 0, 0, int(not engaged), reason)

    monkeypatch.setattr(unfollow_check, 'run_unfollow', run)
    assert unfollow_check.main(['--user-id', str(user), '--account-id', str(account),
                               '--relationship-id', str(relationship), '--worker-id', 'check']) == code
    assert reason in capsys.readouterr().out


def test_cli_failure_does_not_print_private_error(monkeypatch, capsys):
    async def run(*args, **kwargs):
        raise RuntimeError('private-credential-value')
    monkeypatch.setattr(unfollow_check, 'run_unfollow', run)
    assert unfollow_check.main(['--user-id', str(uuid4()), '--account-id', str(uuid4()),
                               '--relationship-id', str(uuid4()), '--worker-id', 'check']) == 2
    assert 'private-credential-value' not in capsys.readouterr().err
