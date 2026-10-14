"""Unresolved Story attempts consume admission capacity without billing."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository
from engagemate.worker.run_context_reader import _USAGE_SQL
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker.test_follow_reel_store import enabled, claim  # noqa: F401
from tests.worker.test_follow_store import NOW, ready, sql  # noqa: F401

@pytest.mark.parametrize('status',['started','uncertain','confirmed','denied'])
@pytest.mark.parametrize('engagement',['none','failed','sent','older_sent'])
async def test_admission_counts_unlinked_hold_once_without_billing(world,status,engagement):  # noqa: F811
    pool = world['pool']
    values = dict(run_id=world['run_id'],job_id=world['job_id'],user_id=world['user_id'],
        ig_account_id=world['account_id'],worker_id='canary-fixture',opportunity_id=uuid4(),
        target_id=world['target_id'],story_fingerprint='ab'*32,action='reply',
        idempotency_key=f'quota_{uuid4().hex}',generated_text_hash=None,
        now=datetime.now(UTC),execution_mode='autonomous')
    repository = ExecutionMcpRepository(pool)
    await repository.begin_mutation(**values)
    now = datetime.now(UTC)+timedelta(seconds=1)
    with pool.connection() as connection:
        connection.execute("UPDATE hermes_tool_calls SET status=%s WHERE run_id=%s",(status,world['run_id']))
        if engagement != 'none':
            created = now-timedelta(seconds=2) if engagement=='older_sent' else now
            connection.execute("INSERT INTO story_engagements(user_id,ig_account_id,target_id,story_fingerprint,action,model_used,status,created_at,executed_at) VALUES(%s,%s,%s,%s,'reply','fixture',%s,%s,%s)",
                (world['user_id'],world['account_id'],world['target_id'],values['story_fingerprint'],
                 'failed' if engagement=='failed' else 'sent',created,now))
        params = {'user_id':world['user_id'],'account_id':world['account_id'],'now':now,
                  'utc_midnight':now.replace(hour=0,minute=0,second=0,microsecond=0)}
        counted = connection.execute(_USAGE_SQL,params).fetchone()['replies']
        held = int(status!='denied')
        assert counted == (1 if engagement=='sent' else held+int(engagement=='older_sent'))
        assert connection.execute(_USAGE_SQL,{**params,'user_id':uuid4()}).fetchone()['replies'] == 0
        assert connection.execute(_USAGE_SQL,{**params,'account_id':world['other_account_id']}).fetchone()['replies'] == 0
        assert connection.execute("SELECT count(*) AS n FROM engagement_usage_events WHERE user_id=%s",(world['user_id'],)).fetchone()['n'] == 0


@pytest.mark.parametrize('boundary',['follow','reel'])
@pytest.mark.parametrize('status',['started','uncertain'])
async def test_shared_private_cap_holds_follow_and_reel_before_any_attempt(enabled,boundary,status):  # noqa: F811
    w=enabled
    sql(w,'UPDATE agent_configs SET daily_reply_limit=1,daily_dm_limit=0 WHERE ig_account_id=%s',(w['account_id'],))
    if boundary=='reel':
        store,message=await claim(w)
    else:
        store=w['store']
        assert await store.enqueue_confirmed(NOW)==1
    sql(w,"INSERT INTO hermes_tool_calls(run_id,tool_name,target_id,story_fingerprint,idempotency_key,status,started_at) VALUES(%s,'reply',%s,%s,%s,%s,%s)",
        (w['run_id'],w['target_id'],'b'*64,str(uuid4()),status,NOW))
    if boundary=='reel':
        assert not await store.prepare(message,'https://www.instagram.com/reel/fixture/','hello',NOW)
        assert not await store.mark_attempting(message,NOW)
        assert sql(w,'SELECT attempted_at FROM follow_reel_messages WHERE id=%s',(message.id,))==[(None,)]
    else:
        assert await store.claim_next('follow',NOW) is None
        assert sql(w,'SELECT status,follow_attempted_at FROM relationship_follows')==[('follow_pending',None)]
    assert sql(w,'SELECT count(*) FROM engagement_usage_events')==[(0,)]


async def test_utc_window_does_not_count_previous_day_or_future_hold(world):  # noqa: F811
    now=datetime.now(UTC)
    params={'user_id':world['user_id'],'account_id':world['account_id'],'now':now,
            'utc_midnight':now.replace(hour=0,minute=0,second=0,microsecond=0)}
    with world['pool'].connection() as connection:
        for at in [params['utc_midnight']-timedelta(seconds=1),now+timedelta(seconds=1)]:
            connection.execute("INSERT INTO hermes_tool_calls(run_id,tool_name,target_id,story_fingerprint,idempotency_key,status,started_at) VALUES(%s,'reply',%s,%s,%s,'uncertain',%s)",
                (world['run_id'],world['target_id'],'b'*64,str(uuid4()),at))
        assert connection.execute(_USAGE_SQL,params).fetchone()['replies']==0
