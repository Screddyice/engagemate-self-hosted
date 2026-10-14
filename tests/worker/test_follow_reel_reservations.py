"""The follow lane reserves messages only after account consent."""
from datetime import timedelta
import pytest
from tests.worker.test_canary_scripts import database_url, world
from tests.worker.test_follow_store import NOW, ready, following, sql

async def test_default_off_does_not_change_follows(ready):
    sql(ready, 'UPDATE agent_configs SET daily_reply_limit=0,daily_dm_limit=0')
    await following(ready)
    assert sql(ready, 'SELECT count(*) FROM follow_reel_messages') == [(0,)]

async def test_opted_in_follow_reserves_and_rotates(ready):
    sql(ready, 'UPDATE agent_configs SET follow_reel_message_enabled=true,follow_reel_message_enabled_at=%s',
        (NOW-timedelta(minutes=1),))
    follow = await following(ready)
    assert sql(ready, 'SELECT relationship_follow_id,status,rotation_slot FROM follow_reel_messages') == [(follow.id,'pending',0)]
    assert sql(ready, 'SELECT next_slot FROM follow_reel_rotation') == [(1,)]

async def test_opted_in_follow_requires_message_budget(ready):
    sql(ready, 'UPDATE agent_configs SET follow_reel_message_enabled=true,follow_reel_message_enabled_at=%s,daily_reply_limit=0,daily_dm_limit=0',
        (NOW-timedelta(minutes=1),))
    await ready['store'].enqueue_confirmed(NOW)
    assert await ready['store'].claim_next('test',NOW) is None

async def test_opt_out_before_follow_receipt_cancels_reservation(ready):
    sql(ready, 'UPDATE agent_configs SET follow_reel_message_enabled=true,follow_reel_message_enabled_at=%s',
        (NOW-timedelta(minutes=1),))
    store=ready['store']
    await store.enqueue_confirmed(NOW)
    claim=await store.claim_next('test',NOW)
    assert await store.mark_attempting(claim,NOW)
    sql(ready, 'UPDATE agent_configs SET follow_reel_message_enabled=false')
    assert await store.finish(claim,'confirmed',NOW,{'confirmed':True})
    assert sql(ready, 'SELECT status FROM follow_reel_messages') == [('cancelled',)]
