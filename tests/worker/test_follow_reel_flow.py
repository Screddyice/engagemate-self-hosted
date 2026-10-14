"""Local contract flow: migrated Postgres through browser adapter and receipt ledger."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from engagemate.worker.follow_reel_browser import FollowReelBrowser
from engagemate.worker.follow_reel_maintenance import ReelMessageMaintenance
from engagemate.worker.follow_reel_store import PostgresFollowReelStore
from engagemate.worker.follow_reels import choose_reel, compose_message
from engagemate.worker.run_context_reader import _USAGE_SQL
from tests.worker.test_canary_scripts import database_url, world
from tests.worker.test_follow_store import NOW, ready, following, sql
from tests.worker.test_follow_reel_store import enabled
from tests.worker.test_follow_reel_reader import response, candidate_payload, native_page, media_detail


@pytest.mark.parametrize('scenario', ['sent', 'lost_response', 'opt_out', 'preflight_expired', 'post_login', 'readback_login'])
async def test_confirmed_follow_to_one_receipted_message(enabled, scenario):
    follow = await following(enabled)
    sql(enabled, "UPDATE targets SET username='recipient'")
    template = sql(enabled, 'SELECT follow_reel_message_template FROM agent_configs')[0][0]
    url = 'https://www.instagram.com/reel/newest/'
    body = compose_message(url, template)
    replies = [media_detail(code,str(i+10),stamp,owner='999') for i,(code,stamp) in enumerate([
        ('oldpin',1600000000),('newest',1700000030),('middle',1700000020),('oldest',1700000010)])]
    replies.insert(0,response({'status':'ok','viewer':{'pk':'999','username':'sender'}}))
    context = SimpleNamespace(request=SimpleNamespace(fetch=AsyncMock(side_effect=replies)),
        cookies=AsyncMock(return_value=[{'name': 'ds_user_id', 'value': '999'},
            {'name': 'csrftoken', 'value': 'test-only'}]))

    async def perform(claim, prepare, before_mutation):
        adapter = FollowReelBrowser(context)
        adapter.page = native_page(candidate_payload(owner='999'),owner='999')
        adapter._inspect_profile = AsyncMock(return_value={'owner_id':'123456','handle':'recipient','following':True})
        await adapter.verify_accounts(claim.sender_id, claim.sender_handle, claim.recipient_id, claim.handle)
        reel = choose_reel(await adapter.read_reels(claim.sender_id), claim.sender_id, claim.rotation_slot)
        message = compose_message(reel.url, claim.message_template)
        assert await prepare(reel.url, message)
        if scenario == 'opt_out':
            sql(enabled, 'UPDATE agent_configs SET follow_reel_message_enabled=false')
        async def ui_send(*, before_mutation, **_kwargs):
            if scenario == 'opt_out':
                assert await before_mutation() is False
                return 'deferred', {'reason': 'message_policy_changed'}
            if scenario == 'preflight_expired':
                return 'deferred', {'phase': 'session_preflight', 'reason': 'instagram_login_required',
                    'health_signal': 'authentication_required', 'http_status': 302,
                    'mutation_attempted': False, 'server_confirmed': False}
            assert await before_mutation() is True
            evidence = {'sender_id': '999', 'recipient_id': '123456',
                'mutation_attempted': True, 'server_confirmed': scenario == 'sent',
                'phase': 'receipt_readback' if scenario == 'readback_login' else 'send_request'}
            if scenario == 'sent':
                evidence.update(item_id='987', thread_id='789')
                return 'sent', evidence
            if scenario in {'post_login', 'readback_login'}:
                evidence.update(reason='instagram_login_required', health_signal='authentication_required',
                    http_status=302)
            else:
                evidence['reason'] = 'message_transport_uncertain'
            return 'uncertain', evidence
        adapter._send_from_profile = AsyncMock(side_effect=ui_send)
        return await adapter.send(sender_id=claim.sender_id, recipient_id=claim.recipient_id,
            reel_url=reel.url, body=message, before_mutation=before_mutation)

    lane = ReelMessageMaintenance(PostgresFollowReelStore(enabled['pool']), perform,
        execution_mode='autonomous', clock=lambda: NOW)
    before_attempt = sql(enabled, 'SELECT clock_timestamp()')[0][0]
    summary = await lane.run_once('flow-test')
    after_attempt = sql(enabled, 'SELECT clock_timestamp()')[0][0]
    assert summary.engaged == (scenario == 'sent')
    assert await lane.run_once('flow-test') is None
    row = sql(enabled, 'SELECT relationship_follow_id,status,reel_url,body,attempted_at,evidence FROM follow_reel_messages')[0]
    assert row[:4] == (follow.id, {'sent': 'sent', 'lost_response': 'uncertain', 'opt_out': 'pending',
        'preflight_expired': 'pending', 'post_login': 'uncertain', 'readback_login': 'uncertain'}[scenario], url, body)
    sends = [call for call in context.request.fetch.call_args_list
             if '/direct_v2/threads/broadcast/' in call.args[0]]
    assert not sends
    if scenario not in {'opt_out', 'preflight_expired'}:
        assert max(NOW, before_attempt) <= row[4] <= max(NOW, after_attempt)
    else:
        assert row[4] is None
    if scenario == 'sent':
        assert row[5]['server_confirmed'] and row[5]['item_id'] == '987'
        assert row[5]['sender_id'] == '999' and row[5]['recipient_id'] == '123456'
    if scenario in {'preflight_expired', 'post_login', 'readback_login'}:
        assert row[5]['reason'] == 'instagram_login_required'
        assert row[5]['http_status'] == 302
        assert row[5]['phase'] == {'preflight_expired': 'session_preflight',
            'post_login': 'send_request', 'readback_login': 'receipt_readback'}[scenario]
        assert row[5]['mutation_attempted'] is (scenario != 'preflight_expired')
    with enabled['pool'].connection() as conn:
        usage = conn.execute(_USAGE_SQL, {'user_id': enabled['user_id'], 'account_id': enabled['account_id'],
            'utc_midnight': NOW.replace(hour=0, minute=0, second=0, microsecond=0), 'now': NOW}).fetchone()
    assert usage['dms'] == 1
    assert sql(enabled, 'SELECT next_slot FROM follow_reel_rotation') == [(1,)]
