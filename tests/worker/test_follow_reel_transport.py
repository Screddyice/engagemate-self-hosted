import json
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest
from engagemate.worker.follow_reel_transport import FollowReelTransport, verified_thread_item
from engagemate.worker.follow_reels import compose_message

URL = 'https://www.instagram.com/reel/abcde/'
BODY = compose_message(URL)


def thread():
    return {'status': 'ok', 'thread': {'thread_id': '789', 'is_group': False,
        'users': [{'pk': '456'}], 'items': [{'item_id': '987', 'user_id': '123',
            'item_type': 'text', 'text': BODY}]}}


def response(payload=None, status=200, headers=None):
    return SimpleNamespace(status=status, headers=headers or {}, json=AsyncMock(return_value=payload), dispose=AsyncMock())


def inbox():
    return response({'status': 'ok', 'viewer': {'pk': '123', 'username': 'sender'}})


def browser(responses, *, preflight=None):
    responses = [inbox() if preflight is None else preflight, *responses]
    context = SimpleNamespace(request=SimpleNamespace(fetch=AsyncMock(side_effect=responses)),
        cookies=AsyncMock(return_value=[{'name': 'ds_user_id', 'value': '123'},
                                      {'name': 'csrftoken', 'value': 'local-test-token'}]))
    adapter = FollowReelTransport(context)
    adapter.sender_id, adapter.recipient_id = '123', '456'
    adapter.sender_handle = 'sender'
    return adapter


@pytest.mark.parametrize('field,value', [('user_id', '999'), ('text', 'wrong'), ('item_id', '999'), ('item_type', 'media')])
def test_readback_binds_exact_item(field, value):
    payload = thread()
    payload['thread']['items'][0][field] = value
    assert not verified_thread_item(payload, thread_id='789', item_id='987', sender_id='123', recipient_id='456', body=BODY)


@pytest.mark.parametrize('change', ['recipient', 'group', 'extra_user', 'thread'])
def test_readback_rejects_wrong_thread(change):
    payload = thread()
    if change == 'recipient': payload['thread']['users'][0]['pk'] = '888'
    if change == 'group': payload['thread']['is_group'] = True
    if change == 'extra_user': payload['thread']['users'].append({'pk': '888'})
    if change == 'thread': payload['thread']['thread_id'] = '888'
    assert not verified_thread_item(payload, thread_id='789', item_id='987', sender_id='123', recipient_id='456', body=BODY)


async def test_one_send_post_then_correlated_get_receipt():
    replies = [response({'status': 'ok', 'payload': {'item_id': '987', 'thread_id': '789'}}), response(thread())]
    adapter = browser(replies)
    arm = AsyncMock(return_value=True)
    result, evidence = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=arm)
    assert result == 'sent' and evidence['server_confirmed'] is True
    calls = adapter.context.request.fetch.call_args_list
    assert [c.kwargs['method'] for c in calls] == ['GET', 'POST', 'GET']
    assert calls[0].args[0].endswith('/direct_v2/inbox/?limit=1')
    assert json.loads(calls[1].kwargs['form']['recipient_users']) == [['456']]
    assert calls[1].kwargs['form']['text'] == BODY
    assert evidence['phase'] == 'receipt_readback'
    assert all(c.kwargs['max_redirects'] == c.kwargs['max_retries'] == 0 for c in calls)
    arm.assert_awaited_once()
    for reply in replies: reply.dispose.assert_awaited_once()


@pytest.mark.parametrize('failure', ['transport', 'redirect', 'rate_limit', 'malformed', 'wrong_readback'])
async def test_uncertain_send_never_repeats_post(failure):
    replies = {
        'transport': [TimeoutError('private')],
        'redirect': [response(status=302, headers={'location': '/accounts/login/'})],
        'rate_limit': [response(status=429)],
        'malformed': [response({'status': 'ok', 'payload': {'item_id': 'invalid'}})],
        'wrong_readback': [response({'status': 'ok', 'payload': {'item_id': '987', 'thread_id': '789'}}), response({'status': 'ok'})],
    }[failure]
    adapter = browser(replies)
    result, evidence = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=AsyncMock(return_value=True))
    assert result == 'uncertain' and evidence['server_confirmed'] is False
    assert sum(c.kwargs['method'] == 'POST' for c in adapter.context.request.fetch.call_args_list) == 1
    if failure == 'rate_limit': assert evidence['health_signal'] == 'rate_limited'
    assert BODY not in str(evidence) and 'private' not in str(evidence)


async def test_policy_refusal_never_posts():
    adapter = browser([])
    result, _ = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=AsyncMock(return_value=False))
    assert result == 'deferred'
    assert [c.kwargs['method'] for c in adapter.context.request.fetch.call_args_list] == ['GET']



async def test_same_adapter_cannot_send_twice_even_if_gate_is_wrong():
    adapter = browser([TimeoutError('unknown delivery')])
    before = AsyncMock(return_value=True)
    await adapter.send(sender_id='123',recipient_id='456',reel_url=URL,body=BODY,before_mutation=before)
    outcome,evidence = await adapter.send(sender_id='123',recipient_id='456',reel_url=URL,body=BODY,before_mutation=before)
    assert outcome == 'deferred' and evidence['reason'] == 'message_already_attempted'
    before.assert_awaited_once()
    assert adapter.context.request.fetch.await_count == 2


async def test_send_uses_current_cookie_and_browser_origin_before_arming():
    adapter = browser([response({'status': 'ok', 'payload': {'item_id': '987', 'thread_id': '789'}}), response(thread())])
    adapter.headers['X-CSRFToken'] = 'stale-test-token'

    async def arm():
        assert adapter.context.cookies.await_count == 2
        assert all(c.args == ('https://www.instagram.com',) for c in adapter.context.cookies.await_args_list)
        assert adapter.context.request.fetch.await_count == 1
        assert adapter.headers['X-CSRFToken'] == 'local-test-token'
        assert adapter.headers['Origin'] == 'https://www.instagram.com'
        assert adapter.headers['Referer'] == 'https://www.instagram.com/direct/inbox/'
        return True

    result, _ = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=arm)
    assert result == 'sent'


@pytest.mark.parametrize('cookies', [[], [{'name': 'ds_user_id', 'value': '999'}, {'name':'csrftoken','value':'test'}],
    [{'name': 'ds_user_id', 'value': '123'}], [{'name':'ds_user_id','value':'123'},{'name':'csrftoken','value':''}]])
async def test_changed_cookie_context_never_arms_or_posts(cookies):
    adapter = browser([])
    adapter.context.cookies.return_value = cookies
    arm = AsyncMock(return_value=True)
    result, evidence = await adapter.send(sender_id='123',recipient_id='456',reel_url=URL,body=BODY,before_mutation=arm)
    assert result == 'deferred' and evidence['reason'] == 'message_session_changed'
    arm.assert_not_awaited()
    adapter.context.request.fetch.assert_not_awaited()


@pytest.mark.parametrize('status,payload,reason,health', [
    (403, {'status':'fail','message':'CSRF token missing or incorrect'}, 'instagram_csrf_rejected', None),
    (403, {'status':'fail','message':'not permitted'}, 'instagram_request_refused', None),
    (401, {'status':'fail','message':'login_required'}, 'message_transport_refused', None),
    (403, {'status':'fail','require_login':True}, 'message_transport_refused', None),
    (400, {'status':'fail','challenge':{} ,'checkpoint_url':'/challenge/'}, 'instagram_checkpoint', 'checkpoint_required'),
])
async def test_rejected_response_preserves_reason_without_false_login_signal(status,payload,reason,health):
    adapter = browser([response(payload,status=status)])
    result, evidence = await adapter.send(sender_id='123',recipient_id='456',reel_url=URL,body=BODY,before_mutation=AsyncMock(return_value=True))
    assert result == 'uncertain'
    assert evidence['reason'] == reason and evidence['http_status'] == status
    assert evidence.get('health_signal') == health
    assert adapter.context.request.fetch.await_count == 2


@pytest.mark.parametrize('preflight,reason,health,status', [
    (response(status=302, headers={'location': '/accounts/login/'}), 'instagram_login_required', 'authentication_required', 302),
    (response({'status': 'fail', 'message': 'login_required'}, status=401), 'instagram_login_required', 'authentication_required', 401),
    (response(status=302, headers={'location': '/challenge/'}), 'instagram_checkpoint', 'checkpoint_required', 302),
    (response(status=302, headers={'location': '/'}), 'instagram_redirected', None, 302),
    (response(status=429), 'instagram_rate_limited', 'rate_limited', 429),
    (response({'status': 'fail', 'message': 'not permitted'}, status=403), 'instagram_request_refused', None, 403),
    (response({'status': 'ok'}), 'sender_identity_mismatch', 'identity_mismatch', 200),
    (response({'status': 'ok', 'viewer': {'pk': '999', 'username': 'sender'}}), 'sender_identity_mismatch', 'identity_mismatch', 200),
    (response({'status': 'ok', 'viewer': {'pk': '123', 'username': 'other'}}), 'sender_identity_mismatch', 'identity_mismatch', 200),
    (TimeoutError('private session detail'), 'message_session_unavailable', None, None),
])
async def test_preflight_failures_never_arm_or_post(preflight, reason, health, status):
    adapter = browser([], preflight=preflight)
    arm = AsyncMock(return_value=True)
    outcome, evidence = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=arm)
    assert outcome == 'deferred'
    assert evidence == {'phase': 'session_preflight', 'mutation_attempted': False,
        'server_confirmed': False, 'reason': reason,
        **({'health_signal': health} if health else {}),
        **({'http_status': status} if status else {})}
    arm.assert_not_awaited()
    assert adapter._send_started is False
    assert [c.kwargs['method'] for c in adapter.context.request.fetch.call_args_list] == ['GET']
    assert 'private' not in str(evidence) and BODY not in str(evidence)
    if not isinstance(preflight, Exception):
        preflight.dispose.assert_awaited_once()


async def test_inbox_cookie_rotation_is_refreshed_before_arming():
    adapter = browser([response({'status': 'ok', 'payload': {'item_id': '987', 'thread_id': '789'}}), response(thread())])
    adapter.context.cookies.side_effect = [
        [{'name': 'ds_user_id', 'value': '123'}, {'name': 'csrftoken', 'value': token}]
        for token in ('before-inbox', 'after-inbox')]

    async def arm():
        assert adapter.context.request.fetch.await_count == 1
        assert adapter.context.cookies.await_count == 2
        assert adapter.headers['X-CSRFToken'] == 'after-inbox'
        return True

    outcome, _ = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=arm)
    assert outcome == 'sent'
    assert adapter.context.request.fetch.call_args_list[1].kwargs['headers']['X-CSRFToken'] == 'after-inbox'


async def test_cookie_identity_changed_by_inbox_never_arms():
    adapter = browser([])
    adapter.context.cookies.side_effect = [
        [{'name': 'ds_user_id', 'value': owner}, {'name': 'csrftoken', 'value': 'test'}]
        for owner in ('123', '999')]
    arm = AsyncMock(return_value=True)
    outcome, evidence = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=arm)
    assert outcome == 'deferred' and evidence['reason'] == 'message_session_changed'
    arm.assert_not_awaited()
    assert adapter.context.request.fetch.await_count == 1


@pytest.mark.parametrize('readback', [False, True])
async def test_login_failure_after_arming_stays_uncertain_with_its_phase(readback):
    replies = [response({'status': 'ok', 'payload': {'item_id': '987', 'thread_id': '789'}})] if readback else []
    replies.append(response(status=302, headers={'location': '/accounts/login/'}))
    adapter = browser(replies)
    arm = AsyncMock(return_value=True)
    outcome, evidence = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=arm)
    assert outcome == 'uncertain' and evidence['mutation_attempted'] is True
    assert evidence['phase'] == ('receipt_readback' if readback else 'send_request')
    assert evidence['reason'] == 'message_transport_refused' and evidence['http_status'] == 302
    assert 'health_signal' not in evidence
    assert ('item_id' in evidence) is readback
    arm.assert_awaited_once()
    assert sum(c.kwargs['method'] == 'POST' for c in adapter.context.request.fetch.call_args_list) == 1


async def test_missing_verified_sender_handle_fails_closed():
    adapter = FollowReelTransport(SimpleNamespace(request=SimpleNamespace(fetch=AsyncMock())))
    adapter.sender_id, adapter.recipient_id = '123', '456'
    arm = AsyncMock(return_value=True)
    outcome, evidence = await adapter.send(sender_id='123', recipient_id='456', reel_url=URL, body=BODY, before_mutation=arm)
    assert outcome == 'deferred' and evidence['reason'] == 'message_identity_unverified'
    arm.assert_not_awaited()
    adapter.context.request.fetch.assert_not_awaited()


@pytest.mark.parametrize('body,expected', [(BODY, True), ('different message', False)])
def test_link_preview_readback_requires_exact_original_text(body,expected):
    payload = thread()
    item = payload['thread']['items'][0]
    item.update(item_type='link',link={'text':body})
    item.pop('text')
    assert verified_thread_item(payload,thread_id='789',item_id='987',sender_id='123',recipient_id='456',body=BODY) is expected
