"""Real migrations: orphan claims stay held, scoped and charged once."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from engagemate.db.pool import create_pool
from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository
from tests.worker.test_canary_scripts import database_url  # noqa: F401
from tests.worker.test_execution_mcp_repository_postgres import world, _mutation, _tool_call as _raw_tool_call  # noqa: F401


def _tool_call(*args):
    from engagemate.db.text import text
    row = _raw_tool_call(*args)
    return {key:text(value) if isinstance(value,bytes) else value for key,value in row.items()}


@pytest.fixture
def pool(database_url):  # noqa: F811
    value = create_pool(database_url,min_size=1,max_size=3)
    yield value
    value.close()


async def test_phase_transition_requires_exact_current_owner_and_live_run(pool,world):  # noqa: F811
    repository,values = ExecutionMcpRepository(pool),_mutation(world)
    assert (await repository.begin_mutation(**values))["allowed"]
    assert _tool_call(pool,world['run_id'],values['idempotency_key'])['safe_result']['phase'] == 'reserved'
    params = dict(run_id=world['run_id'],idempotency_key=values['idempotency_key'],
                  worker_id='worker-1',user_id=world['user_id'],account_id=world['account_id'],completed_at=datetime.now(UTC))
    assert not await repository.mark_execution_started(**{**params,'worker_id':'foreign'})
    assert not await repository.mark_execution_started(**{**params,'user_id':uuid4()})
    # The earlier caller timestamp cannot authorize a lease expired at SQL time.
    with pool.connection() as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(world['job_id'],))
    assert not await repository.mark_execution_started(**{**params,'completed_at':datetime.now(UTC)-timedelta(minutes=1)})
    with pool.connection() as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=now()+interval '5 minutes' WHERE id=%s",(world['job_id'],))
    assert await repository.mark_execution_started(**params)
    assert _tool_call(pool,world['run_id'],values['idempotency_key'])['safe_result']['phase'] == 'execution_started'
    await repository.fail_run(run_id=world['run_id'],reason='canary_timeout',completed_at=datetime.now(UTC))
    assert not await repository.mark_execution_started(**params)


async def test_lock_wait_rechecks_current_lease_before_arming(pool,world):  # noqa: F811
    repository,values = ExecutionMcpRepository(pool),_mutation(world)
    assert (await repository.begin_mutation(**values))['allowed']
    with pool.connection() as locked, locked.transaction():
        holder = locked.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        locked.execute('SELECT id FROM hermes_tool_calls WHERE run_id=%s FOR UPDATE',(world['run_id'],))
        arming = asyncio.create_task(repository.mark_execution_started(
            run_id=world['run_id'],idempotency_key=values['idempotency_key'],worker_id='worker-1',
            user_id=world['user_id'],account_id=world['account_id'],completed_at=datetime.now(UTC)))
        try:
            async with asyncio.timeout(2):
                while True:
                    with pool.connection() as inspect:
                        blocked = inspect.execute('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND %s=ANY(pg_blocking_pids(pid))) AS blocked',(holder,)).fetchone()['blocked']
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
            with pool.connection() as inspect:
                inspect.execute("UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(world['job_id'],))
            assert not arming.done()
        except BaseException:
            arming.cancel()
            raise
    assert not await asyncio.wait_for(arming,2)


async def test_failed_terminal_run_sweeps_legacy_started_without_replay_and_late_receipt_wins(pool,world):  # noqa: F811
    repository,values = ExecutionMcpRepository(pool),_mutation(world)
    await repository.begin_mutation(**values)
    with pool.connection() as connection:
        connection.execute("UPDATE hermes_tool_calls SET safe_result='{}' WHERE run_id=%s",(world['run_id'],))
        connection.execute("UPDATE jobs SET status='failed' WHERE id=%s",(world['job_id'],))
    await repository.fail_run(run_id=world['run_id'],reason='canary_timeout',completed_at=datetime.now(UTC))
    assert await repository.reconcile_abandoned_runs() == 1
    row = _tool_call(pool,world['run_id'],values['idempotency_key'])
    assert row['status'] == 'uncertain' and row['failure_class'] == 'worker_interrupted'
    assert await repository.reconcile_abandoned_runs() == 0
    await repository.finish_mutation(run_id=world['run_id'],idempotency_key=values['idempotency_key'],
        status='confirmed',failure_class=None,safe_result={'status':'confirmed'},completed_at=datetime.now(UTC))
    assert _tool_call(pool,world['run_id'],values['idempotency_key'])['status'] == 'confirmed'
    assert await repository.reconcile_abandoned_runs() == 0


async def test_another_run_and_target_cannot_replay_held_story_fingerprint(pool,world):  # noqa: F811
    repository,values = ExecutionMcpRepository(pool),_mutation(world)
    assert (await repository.begin_mutation(**values))['allowed']
    run_id,target,job_id = uuid4(),uuid4(),uuid4()
    with pool.connection() as connection:
        connection.execute("INSERT INTO jobs(id,user_id,ig_account_id,schedule_key,scheduled_for,status,claimed_by,lease_expires_at) VALUES(%s,%s,%s,%s,now(),'running','worker-1',now()+interval '5 minutes')",(job_id,world['user_id'],world['account_id'],str(job_id)))
        connection.execute("INSERT INTO hermes_execution_runs (id,job_id,user_id,ig_account_id,hermes_session_id,config_version,config_snapshot,status) VALUES (%s,%s,%s,%s,%s,1,'{}','running')",
            (run_id,job_id,world['user_id'],world['account_id'],f'em_{run_id.hex}'))
        connection.execute("INSERT INTO targets(id,user_id,ig_account_id,username,tier,source) VALUES(%s,%s,%s,%s,'vip','manual')",
            (target,world['user_id'],world['account_id'],f'target_{target.hex}'))
    attempted = _mutation(world,run_id=run_id,target_id=target,job_id=job_id)
    assert (await repository.begin_mutation(**attempted))['reason'] == 'unresolved_story_action'
    assert _tool_call(pool,run_id,attempted['idempotency_key'])['status'] == 'denied'
