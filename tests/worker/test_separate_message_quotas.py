"""Independent lanes reserve messages before likes and retain write holds."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4
import pytest
from engagemate.domain.budgets import (
    AccountBudgetState, AgentConfigLimits, BudgetPolicy, DailyActionUsage,
    daily_window_start,
)
from engagemate.worker.follow_reel_store import PostgresFollowReelStore
from engagemate.worker.run_context import UsageRow
from tests.worker.test_follow_reel_store import enabled, claim
from tests.worker.test_follow_store import NOW, ready, sql
from tests.worker.test_canary_scripts import database_url, world

from tests.worker.test_message_quota_policy import CLOCK, CONFIG, ACCOUNT

async def test_story_loader_keeps_three_replies_and_three_reels():
    from tests.worker.test_run_context import account_row, rows, FakeReader, build_loader, claimed_job, USER, ACCOUNT as AID
    account = account_row(plan='starter', daily_like_limit=8, daily_reply_limit=3, daily_dm_limit=0,
                          daily_reel_message_limit=3, warmup_exempt=True,
                          follow_after_engagement=True, follow_reel_message_enabled=True)
    loader,_,_ = build_loader(reader=FakeReader(rows(account=account,usage=UsageRow(USER,AID,0,0,0))))
    context = await loader.load(claimed_job())
    assert (context.budget.replies,context.budget.private_messages,context.budget.reel_messages,context.budget.likes)==(3,3,3,5)

async def test_three_story_receipts_do_not_block_a_reel(enabled):
    sql(enabled, "UPDATE agent_configs SET daily_reply_limit=3,daily_reel_message_limit=3,warmup_exempt=true")
    sql(enabled, "UPDATE ig_accounts SET warmup_started_at=%s", (NOW-timedelta(days=1),))
    store, message = await claim(enabled)
    for i in range(3):
        sql(enabled,"INSERT INTO story_engagements(user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at) VALUES (%s,%s,%s,%s,'reply','sent',%s)",
            (enabled['user_id'],enabled['account_id'],enabled['target_id'],str(i)*64,NOW))
    assert await store.prepare(message,'https://www.instagram.com/reel/abc123/','hello\nhttps://www.instagram.com/reel/abc123/',NOW)
    assert await store.mark_attempting(message,NOW)

@pytest.mark.parametrize('status', ['pending','running','sent','uncertain'])
async def test_three_reel_holds_leave_story_capacity_but_block_another_reel(enabled, status):
    sql(enabled,"UPDATE agent_configs SET daily_reply_limit=3,daily_reel_message_limit=3,warmup_exempt=true")
    store,message=await claim(enabled)
    for i in range(3):
        target, relationship, engagement = uuid4(), uuid4(), uuid4()
        owner = str(880000 + i)
        sql(enabled, "INSERT INTO targets(id,user_id,ig_account_id,username,ig_user_id,tier,source) VALUES (%s,%s,%s,%s,%s,'broad','manual')",
            (target,enabled['user_id'],enabled['account_id'],f'holder{i}',owner))
        sql(enabled, "INSERT INTO story_engagements(id,user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at) VALUES (%s,%s,%s,%s,%s,'like','sent',%s)",
            (engagement,enabled['user_id'],enabled['account_id'],target,str(i)*64,NOW-timedelta(days=1)))
        sql(enabled, "INSERT INTO relationship_follows(id,user_id,ig_account_id,target_id,owner_id,prerequisite_engagement_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (relationship,enabled['user_id'],enabled['account_id'],target,owner,engagement))
        sql(enabled, """INSERT INTO follow_reel_messages(relationship_follow_id,user_id,ig_account_id,target_id,recipient_id,status,reserved_at,rotation_slot,reel_url,body,attempted_at,sent_at,claimed_by,lease_token,lease_expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,0,'https://www.instagram.com/reel/abc123/','hello',%s,%s,%s,%s,%s)""",
            (relationship,enabled['user_id'],enabled['account_id'],target,owner,status,NOW,
             NOW if status in ('sent','uncertain') else None, NOW if status=='sent' else None,
             'holder' if status=='running' else None,uuid4() if status=='running' else None,
             NOW-timedelta(seconds=1) if status=='running' else None))
    assert not await store.prepare(message,'https://www.instagram.com/reel/abc123/','hello\nhttps://www.instagram.com/reel/abc123/',NOW)
    assert not await store.mark_attempting(message,NOW)
    sql(enabled,"UPDATE follow_reel_messages SET status='cancelled',claimed_by=NULL,lease_token=NULL,lease_expires_at=NULL WHERE id=%s",(message.id,))
    from engagemate.worker.run_context_reader import _USAGE_SQL
    with enabled['pool'].connection() as connection:
        used=connection.execute(_USAGE_SQL,{'user_id':enabled['user_id'],'account_id':enabled['account_id'],
            'now':NOW,'utc_midnight':daily_window_start(NOW)}).fetchone()
    budget=BudgetPolicy.remaining(ACCOUNT,CONFIG,DailyActionUsage(used['likes'],used['replies'],used['dms'],reel_messages=used['reel_messages']),CLOCK)
    assert budget.private_messages == budget.replies == 3
    assert budget.reel_messages == 0


def test_exemption_is_audited_and_cannot_be_set_by_the_customer_mcp(world):
    from engagemate.mcp.control_repository import ControlRepository
    repository=ControlRepository(world['pool'])
    before=sql(world,'SELECT version,daily_reply_limit FROM agent_configs WHERE ig_account_id=%s',(world['account_id'],))[0]
    with pytest.raises(ValueError,match='requires an operator'):
        repository.apply_config_patch(world['user_id'],world['account_id'],{'warmup_exempt':True},'mcp','customer','limits.update')
    saved=repository.apply_config_patch(world['user_id'],world['account_id'],
        {'warmup_exempt':True,'daily_reply_limit':3,'daily_reel_message_limit':3},'system','authorized-operator','limits.update')
    assert saved['version']==before[0]+1
    assert sql(world,'SELECT actor_type FROM agent_config_versions WHERE ig_account_id=%s',(world['account_id'],))==[('system',)]
    assert sql(world,'SELECT count(*) FROM agent_configs WHERE ig_account_id=%s AND warmup_exempt',(world['other_account_id'],))==[(0,)]
