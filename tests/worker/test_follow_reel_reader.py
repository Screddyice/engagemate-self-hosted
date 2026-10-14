import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest
from engagemate.worker.follow_reel_reader import (
    FollowReelBrowser, ReelBrowserError, _inbox_message_receipt, _message_response_failure,
    _nested_value,
    verified_thread_item,
)
from engagemate.worker.follow_reels import compose_message

URL = 'https://www.instagram.com/reel/abcde/'
BODY = compose_message(URL)


def thread():
    return {'status': 'ok', 'thread': {'thread_id': '789', 'is_group': False,
        'users': [{'pk': '456'}], 'items': [{'item_id': '987', 'user_id': '123',
            'item_type': 'text', 'text': BODY}]}}


def response(payload=None, status=200, headers=None):
    return SimpleNamespace(status=status, headers=headers or {}, json=AsyncMock(return_value=payload),
        body=AsyncMock(return_value=json.dumps(payload).encode() if payload is not None else b""),
        dispose=AsyncMock())


def browser(responses):
    context = SimpleNamespace(request=SimpleNamespace(fetch=AsyncMock(side_effect=responses)),
        cookies=AsyncMock(return_value=[{'name': 'ds_user_id', 'value': '123'},
                                      {'name': 'csrftoken', 'value': 'local-test-token'}]))
    adapter = FollowReelBrowser(context)
    adapter.sender_id, adapter.recipient_id = '123', '456'
    adapter.sender_handle = 'sender'
    return adapter


async def test_verify_sender_and_recipient_require_follow_relationship(monkeypatch):
    adapter = browser([response({'status':'ok','viewer':{'pk':'123','username':'sender'}})])
    inspect = AsyncMock(return_value={'owner_id': '456', 'handle': 'recipient', 'following': True})
    monkeypatch.setattr(adapter, '_inspect_profile', inspect)
    await adapter.verify_accounts('123', 'sender', '456', 'recipient')
    assert adapter.recipient_id == '456'
    assert inspect.await_args_list[0].args == ('recipient', '456')
    assert adapter.context.request.fetch.call_args.args[0].endswith('/direct_v2/inbox/?limit=1')


async def test_wrong_cookie_sender_never_requests():
    adapter = browser([])
    adapter.context.cookies.return_value = [{'name': 'ds_user_id', 'value': '999'}]
    with pytest.raises(ReelBrowserError, match='sender_identity_mismatch'):
        await adapter.verify_accounts('123', 'sender', '456', 'recipient')
    adapter.context.request.fetch.assert_not_called()


def candidate_payload(owner='123'):
    return {'data': {'fetch__XDTUserDict': {'clips_connection': {
        'edges': [{'node': {'media': {'code': code, 'pk': str(i+10),
             'product_type': 'clips', 'user': {'pk': owner},
             'clips_tab_pinned_user_ids': [owner] if i == 0 else []}}}
            for i, code in enumerate(['oldpin', 'newest', 'middle', 'oldest'])],
        'page_info': {'has_next_page': False}}}}}


def native_page(payload, owner='123', next_payload=None, *, status=200):
    callbacks = []
    page = SimpleNamespace(url='https://www.instagram.com/sender/reels/', close=AsyncMock())
    page.on = lambda event, callback: callbacks.append(callback)
    page.remove_listener = lambda event, callback: callbacks.remove(callback)
    page.locator = lambda selector: SimpleNamespace(count=AsyncMock(return_value=0),
        last=SimpleNamespace(wait_for=AsyncMock(),scroll_into_view_if_needed=AsyncMock()))
    async def emit(body, name, variables):
        r = response(body, status=status)
        r.url = 'https://www.instagram.com/graphql/query'
        r.request = SimpleNamespace(post_data=urlencode({
            'fb_api_req_friendly_name': name, 'doc_id':'fixture-read-query',
            'variables': json.dumps(variables)}))
        for callback in callbacks: callback(r)
        return response(status=200)
    async def navigate(*args, **kwargs):
        return await emit(payload,'PolarisProfileReelsTabContentQuery',
                          {'user_id':owner,'data':{'target_user_id':owner}})
    async def scroll(*args):
        assert next_payload is not None
        return await emit(next_payload,'PolarisProfileReelsTabContentQuery_connection',
            {'id':owner,'data':{'target_user_id':owner},
             'after':payload['data']['fetch__XDTUserDict']['clips_connection']['page_info']['end_cursor']})
    page.mouse = SimpleNamespace(wheel=scroll)
    page.goto = navigate
    return page


@pytest.mark.parametrize("payload,reason,health", [
    ({"message": "csrf_failed"}, "response_failed", None),
    (candidate_payload(), "response_failed", None),
    ({"message": "login_required"}, "authentication_required", "authentication_required"),
    ({"challenge": {"url": "hidden"}}, "checkpoint_required", "checkpoint_required"),
    ({"message": "Please wait a few minutes"}, "rate_limited", "rate_limited"),
])
async def test_native_reel_forbidden_never_selects_reels_or_false_login(payload, reason, health):
    adapter = browser([])
    adapter.page = native_page(payload, status=403)
    with pytest.raises(ReelBrowserError) as error:
        await adapter.read_reels('123')
    assert error.value.reason == reason and error.value.health_signal == health
    adapter.context.request.fetch.assert_not_called()
    assert adapter.page is None


def media_detail(code, media_id, stamp, owner='123'):
    return response({'status': 'ok', 'items': [{'pk': media_id, 'code': code,
        'taken_at': stamp, 'product_type': 'clips', 'user': {'pk': owner}}]})


async def test_native_grid_gets_separate_timestamped_details_without_clicking_message():
    adapter = browser([media_detail(code, str(i+10), stamp) for i,(code,stamp) in enumerate([
        ('oldpin', 1600000000), ('newest', 1700000030), ('middle', 1700000020), ('oldest',1700000010)])])
    adapter.page = native_page(candidate_payload())
    reels = await adapter.read_reels('123')
    from engagemate.worker.follow_reels import choose_reel
    assert choose_reel(reels, '123', 0).code == 'newest'
    calls = adapter.context.request.fetch.call_args_list
    assert len(calls) == 4 and all(c.kwargs['method'] == 'GET' for c in calls)
    assert all('/api/v1/media/' in c.args[0] for c in calls)


async def test_changed_media_fails_before_any_send():
    stamps = [1600000000,1700000030,1700000020,1700000010]
    details = [media_detail(code, str(i+10), stamps[i]) for i,code in enumerate(['oldpin','newest','middle','oldest'])]
    details[0] = media_detail('alien1', '10', stamps[0])
    adapter = browser(details)
    adapter.page = native_page(candidate_payload())
    with pytest.raises(ReelBrowserError, match='reel_detail_unverified'):
        await adapter.read_reels('123')
    assert all(c.kwargs['method'] == 'GET' for c in adapter.context.request.fetch.call_args_list)


@pytest.mark.parametrize('path,name,target,allowed', [
    ('/graphql/query','PolarisProfileReelsTabContentQuery','123',True),
    ('/api/graphql','PolarisProfileReelsTabContentQuery','999',False),
    ('/api/graphql','IGDirectStoryShareReplyMutation','123',False),
    ('/api/v1/direct_v2/create_group_thread/','create','123',False),
])
async def test_read_guard_blocks_all_message_writes_and_other_account_reels(path,name,target,allowed):
    adapter = browser([])
    adapter._profile_target = ('sender','123')
    route = SimpleNamespace(request=SimpleNamespace(url='https://www.instagram.com'+path,
        method='POST',resource_type='fetch',post_data=urlencode({'fb_api_req_friendly_name':name,
        'variables':json.dumps({'user_id':target,'data':{'target_user_id':target}})})),
        continue_=AsyncMock(), abort=AsyncMock())
    await adapter._guard(route)
    assert route.continue_.await_count == int(allowed)
    assert route.abort.await_count == int(not allowed)


async def test_read_guard_aborts_obsolete_profile_endpoint():
    adapter = browser([])
    adapter._profile_target = ('sender','123')
    route = SimpleNamespace(request=SimpleNamespace(url='https://www.instagram.com/api/v1/users/web_profile_info/?username=sender',
        method='GET',resource_type='fetch'), continue_=AsyncMock(), abort=AsyncMock())
    await adapter._guard(route)
    route.abort.assert_awaited_once()


async def test_recipient_no_longer_followed_defers(monkeypatch):
    adapter = browser([response({'status':'ok','viewer':{'pk':'123','username':'sender'}})])
    monkeypatch.setattr(adapter,'_inspect_profile',AsyncMock(return_value=
        {'owner_id':'456','handle':'recipient','following':False}))
    with pytest.raises(ReelBrowserError,match='recipient_no_longer_followed'):
        await adapter.verify_accounts('123','sender','456','recipient')
    assert adapter.context.request.fetch.await_count == 1


class _MessageItem:
    async def get_attribute(self, name):
        return "987" if name in {"data-item-id", "data-message-id"} else None

    async def text_content(self):
        return BODY


class _MessageLocator:
    def __init__(self, page, *, item=False):
        self.page, self.item = page, item

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def fill(self, value):
        assert value == BODY

    async def click(self, **_kwargs):
        if self.item:
            self.page.body_count = 1
            if self.page.thread_url:
                self.page.url = "https://www.instagram.com/direct/t/789/"
            request = object()
            self.page.adapter._message_forwarded = request
            event = response({"data": {"xdt_send_message": {"message_id": "987"}}})
            event.url = "https://www.instagram.com/api/graphql"
            event.request = request
            for callback in tuple(self.page.callbacks):
                callback(event)

    def nth(self, _index):
        return _MessageItem()


class _MessagePage:
    def __init__(self, *, thread_url=True):
        self.url = "https://www.instagram.com/recipient/"
        self.body_count = 0
        self.callbacks = []
        self.adapter = None
        self.thread_url = thread_url

    def on(self, _event, callback):
        self.callbacks.append(callback)

    def remove_listener(self, _event, callback):
        self.callbacks.remove(callback)

    async def wait_for_timeout(self, _milliseconds):
        pass

    def get_by_role(self, _role, *, name, exact=False):
        if hasattr(name, "match"):
            is_send = bool(name.match("Send"))
        else:
            is_send = name == "Send"
        if not is_send and (exact or not hasattr(name, "match") or not name.match("Messages")):
            locator = _MessageLocator(self)
            locator.count = AsyncMock(return_value=0)
            return locator
        return _MessageLocator(self, item=is_send)

    def get_by_text(self, _text, *, exact):
        assert exact
        locator = _MessageLocator(self)
        locator.count = AsyncMock(return_value=self.body_count)
        return locator

    def locator(self, selector):
        return _MessageLocator(self, item=selector.startswith("[data-item-id"))


async def test_send_uses_web_ui_and_never_private_direct_api(monkeypatch):
    adapter = browser([response({'status': 'ok', 'viewer': {'pk': '123', 'username': 'sender'}}), response(thread())])
    adapter.sender_id, adapter.recipient_id = "123", "456"
    adapter.sender_handle, adapter.recipient_handle = "sender", "recipient"
    adapter.page = _MessagePage()
    adapter.page.adapter = adapter
    monkeypatch.setattr(adapter, "_inspect_profile", AsyncMock(return_value={
        "owner_id": "456", "handle": "recipient", "following": True,
    }))
    outcome, evidence = await adapter.send(
        sender_id="123", recipient_id="456", reel_url=URL, body=BODY,
        before_mutation=AsyncMock(return_value=True),
    )
    assert outcome == "sent"
    assert evidence["transport"] == "instagram_message_ui"
    assert evidence["server_confirmed"] is True
    calls = adapter.context.request.fetch.call_args_list
    assert len(calls) == 2
    assert all(call.kwargs["method"] == "GET" for call in calls)
    assert calls[0].args[0].endswith("/direct_v2/inbox/?limit=1")
    assert "/direct_v2/threads/789/" in calls[1].args[0]


async def test_send_reads_inbox_when_ui_receipt_omits_thread(monkeypatch):
    inbox = {
        "status": "ok",
        "inbox": [{
            "thread_id": "789", "is_group": False, "users": [{"pk": "456"}],
            "items": [{"item_id": "987", "user_id": "123", "item_type": "text", "text": BODY}],
        }],
    }
    adapter = browser([
        response({'status': 'ok', 'viewer': {'pk': '123', 'username': 'sender'}}),
        response(inbox), response(thread()),
    ])
    adapter.sender_id, adapter.recipient_id = "123", "456"
    adapter.sender_handle, adapter.recipient_handle = "sender", "recipient"
    adapter.page = _MessagePage(thread_url=False)
    adapter.page.adapter = adapter
    monkeypatch.setattr(adapter, "_inspect_profile", AsyncMock(return_value={
        "owner_id": "456", "handle": "recipient", "following": True,
    }))
    outcome, evidence = await adapter.send(
        sender_id="123", recipient_id="456", reel_url=URL, body=BODY,
        before_mutation=AsyncMock(return_value=True),
    )
    assert outcome == "sent"
    assert evidence["server_confirmed"] is True
    assert evidence["thread_id"] == "789" and evidence["item_id"] == "987"
    calls = adapter.context.request.fetch.call_args_list
    assert calls[1].args[0].endswith("/direct_v2/inbox/?limit=100")
    assert "/direct_v2/threads/789/" in calls[2].args[0]


async def test_message_control_failure_before_send_is_deferred(monkeypatch):
    from engagemate.worker import follow_reel_reader

    wait = follow_reel_reader._one_visible

    async def bounded_wait(locator):
        return await wait(locator, timeout=0.001)

    monkeypatch.setattr(follow_reel_reader, "_one_visible", bounded_wait)
    adapter = browser([response({'status': 'ok', 'viewer': {'pk': '123', 'username': 'sender'}})])
    adapter.sender_id, adapter.recipient_id = "123", "456"
    adapter.sender_handle, adapter.recipient_handle = "sender", "recipient"
    adapter.page = _MessagePage()
    adapter.page.adapter = adapter
    monkeypatch.setattr(adapter, "_inspect_profile", AsyncMock(return_value={
        "owner_id": "456", "handle": "recipient", "following": True,
    }))
    adapter.page.get_by_role = lambda *_args, **_kwargs: SimpleNamespace(
        count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=False),
    )
    adapter.page.locator = lambda *_args, **_kwargs: SimpleNamespace(
        count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=False),
    )
    outcome, evidence = await adapter.send(
        sender_id="123", recipient_id="456", reel_url=URL, body=BODY,
        before_mutation=AsyncMock(return_value=True),
    )
    assert outcome == "deferred"
    assert evidence["mutation_attempted"] is False
    assert evidence["reason"] in {
        "message_composer_unavailable", "message_send_control_unavailable",
    }


async def test_message_guard_rejects_private_direct_api_post():
    adapter = browser([])
    adapter._message_target = ("recipient", "456")
    adapter._message_body = BODY
    adapter._message_armed = True
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://www.instagram.com/api/v1/direct_v2/threads/broadcast/text/",
            method="POST", resource_type="fetch", post_data="text=private",
        ), continue_=AsyncMock(), abort=AsyncMock(),
    )
    await adapter._guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


async def test_message_guard_rejects_unarmed_direct_mutation():
    adapter = browser([])
    adapter._message_target = ("recipient", "456")
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://www.instagram.com/api/graphql",
            method="POST", resource_type="fetch",
            post_data=urlencode({"fb_api_req_friendly_name": "PolarisDirectSendMessageMutation",
                "variables": json.dumps({"text": BODY})}),
        ), continue_=AsyncMock(), abort=AsyncMock(),
    )
    await adapter._guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


@pytest.mark.parametrize("armed,recipients,allowed", [
    (False, ["456"], True),
    (False, ["999"], False),
    (True, ["456"], False),
])
async def test_message_guard_only_allows_exact_thread_bootstrap_before_send(
    armed, recipients, allowed,
):
    adapter = browser([])
    adapter._message_target = ("recipient", "456")
    adapter._message_armed = armed
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://www.instagram.com/api/v1/direct_v2/create_group_thread/",
            method="POST", resource_type="fetch",
            post_data=urlencode({
                "recipient_users": json.dumps(recipients),
                "fb_dtsg": "fixture-token",
                "jazoest": "12345",
            }),
        ), continue_=AsyncMock(), abort=AsyncMock(),
    )
    await adapter._guard(route)
    assert route.continue_.await_count == int(allowed)
    assert route.abort.await_count == int(not allowed)


@pytest.mark.parametrize("name", [
    "IGDChatTabsContentOffMsysQuery",
    "IGDChatTabRootOffMsysQuery",
    "IGPresenceUnifiedSetupQuery",
    "PolarisViewerSettingsQuery",
    "useFeatureLimitsOffMsysQuery",
    "QuickPromotionSupportIGSchemaBatchFetchQuery",
    "PolarisAPIGetFrCookieQuery",
    "IGDChatTabsJewelOffMsysQuery",
    "IGDOmniPickerNullStateListQuery",
])
async def test_message_guard_allows_composer_bootstrap_reads_before_send(name):
    adapter = browser([])
    adapter._message_target = ("recipient", "456")
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://www.instagram.com/api/graphql", method="POST",
            resource_type="fetch", post_data=urlencode({
                "fb_api_req_friendly_name": name,
                "variables": json.dumps({}),
            }),
        ), continue_=AsyncMock(), abort=AsyncMock(),
    )
    await adapter._guard(route)
    route.continue_.assert_awaited_once()
    route.abort.assert_not_awaited()


async def test_message_guard_rejects_composer_reads_after_send_armed():
    adapter = browser([])
    adapter._message_target = ("recipient", "456")
    adapter._message_armed = True
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://www.instagram.com/api/graphql", method="POST",
            resource_type="fetch", post_data=urlencode({
                "fb_api_req_friendly_name": "IGDChatTabRootOffMsysQuery",
                "variables": json.dumps({}),
            }),
        ), continue_=AsyncMock(), abort=AsyncMock(),
    )
    await adapter._guard(route)
    route.abort.assert_awaited_once()
    route.continue_.assert_not_awaited()


async def test_message_guard_forwards_one_exact_polaris_send():
    adapter = browser([])
    adapter._message_target = ("recipient", "456")
    adapter._message_body = BODY
    adapter._message_armed = True
    variables = {
        "ig_thread_igid": "789", "offline_threading_id": "987",
        "recipient_igids": ["456"], "text": {"sensitive_string_value": BODY},
    }
    route = SimpleNamespace(
        request=SimpleNamespace(
            url="https://www.instagram.com/api/graphql", method="POST", resource_type="fetch",
            post_data=urlencode({"fb_api_req_friendly_name": "IGDirectTextSendMutation",
                "variables": json.dumps(variables)}),
        ), continue_=AsyncMock(), abort=AsyncMock(),
    )
    await adapter._guard(route)
    route.continue_.assert_awaited_once()
    route.abort.assert_not_awaited()
    second = SimpleNamespace(
        request=route.request, continue_=AsyncMock(), abort=AsyncMock(),
    )
    await adapter._guard(second)
    second.abort.assert_awaited_once()
    second.continue_.assert_not_awaited()


def test_message_login_redirect_is_a_health_signal():
    error = _message_response_failure(302, {"location": "/accounts/login/?next=/direct/"}, None)
    assert error.reason == "instagram_login_required"
    assert error.health_signal == "authentication_required"


def test_verified_session_login_redirect_is_transport_failure():
    error = _message_response_failure(
        302, {"location": "/accounts/login/?next=/direct/"}, None, session_verified=True,
    )
    assert error.reason == "message_transport_refused"
    assert error.health_signal is None


def test_message_readback_does_not_use_short_graphql_thread_key():
    assert _nested_value({"thread_fbid": "123"}, ("thread_id", "threadId", "thread_v2_id")) is None


def test_inbox_message_receipt_deduplicates_nested_item():
    payload = {
        "status": "ok",
        "inbox": [{
            "thread_id": "789", "is_group": False, "users": [{"pk": "456"}],
            "items": [{"item_id": "987", "user_id": "123", "item_type": "text", "text": BODY}],
        }],
    }
    receipt = _inbox_message_receipt(payload, sender_id="123", recipient_id="456", body=BODY)
    assert receipt == {"thread_id": "789", "item_id": "987"}


def test_inbox_message_receipt_rejects_ambiguous_items():
    payload = {
        "status": "ok",
        "inbox": [
            {"thread_id": "789", "is_group": False, "users": [{"pk": "456"}],
             "items": [{"item_id": "987", "user_id": "123", "item_type": "text", "text": BODY}]},
            {"thread_id": "790", "is_group": False, "users": [{"pk": "456"}],
             "items": [{"item_id": "988", "user_id": "123", "item_type": "text", "text": BODY}]},
        ],
    }
    assert _inbox_message_receipt(
        payload, sender_id="123", recipient_id="456", body=BODY,
    ) is None


async def test_ui_send_cannot_be_armed_twice(monkeypatch):
    adapter = browser([response({'status': 'ok', 'viewer': {'pk': '123', 'username': 'sender'}}), response(thread())])
    adapter.sender_handle, adapter.recipient_handle = "sender", "recipient"
    adapter.page = _MessagePage()
    adapter.page.adapter = adapter
    monkeypatch.setattr(adapter, "_inspect_profile", AsyncMock(return_value={
        "owner_id": "456", "handle": "recipient", "following": True,
    }))
    gate = AsyncMock(return_value=True)
    assert (await adapter.send(sender_id="123", recipient_id="456", reel_url=URL,
        body=BODY, before_mutation=gate))[0] == "sent"
    result, evidence = await adapter.send(sender_id="123", recipient_id="456", reel_url=URL,
        body=BODY, before_mutation=gate)
    assert result == "deferred" and evidence["reason"] == "message_already_attempted"
    gate.assert_awaited_once()


async def test_native_grid_order_cannot_override_publication_dates():
    adapter=browser([media_detail(code,str(i+10),stamp) for i,(code,stamp) in enumerate([
        ('oldpin',1600000000),('newest',1700000010),('middle',1700000040),('oldest',1700000020)])])
    adapter.page=native_page(candidate_payload())
    reels=await adapter.read_reels('123')
    from engagemate.worker.follow_reels import choose_reel
    assert [choose_reel(reels,'123',i).code for i in range(3)] == ['middle','oldest','newest']

async def test_exhausts_measured_pagination_before_sorting_later_newer_reel():
    from copy import deepcopy
    from engagemate.worker.follow_reels import choose_reel
    first=candidate_payload()
    first['data']['fetch__XDTUserDict']['clips_connection']['page_info']={'has_next_page':True,'end_cursor':'cursor1'}
    second=deepcopy(first)
    second['data']['fetch__XDTUserDict']['clips_connection']['page_info']['end_cursor']='cursor2'
    third=candidate_payload()
    connection=third['data']['fetch__XDTUserDict']['clips_connection']
    connection['edges']=connection['edges'][:1]
    connection['edges'][0]['node']['media'].update(pk='99',code='later1',clips_tab_pinned_user_ids=[])
    reads=[response(third)]+[media_detail(code,str(i+10),1700000000+i) for i,code in enumerate(['oldpin','newest','middle','oldest'])]
    reads.append(media_detail('later1','99',1700000100))
    adapter=browser(reads)
    adapter.page=native_page(first,next_payload=second)
    reels=await adapter.read_reels('123')
    assert choose_reel(reels,'123',0).code=='later1'
    calls=adapter.context.request.fetch.call_args_list
    assert calls[0].args[0].endswith('/graphql/query')
    assert json.loads(calls[0].kwargs['form']['variables'])['after']=='cursor2'
    assert json.loads(calls[0].kwargs['form']['variables'])['first']==12
    assert len(calls)==6 and all(c.kwargs['method']=='GET' for c in calls[1:])


@pytest.mark.parametrize('payload,health', [
    ({'message': 'csrf_failed'}, None),
    ({'message': 'login_required'}, 'authentication_required'),
    ({'challenge': {'url': 'hidden'}}, 'checkpoint_required'),
    ({'message': 'Please wait a few minutes'}, 'rate_limited'),
])
async def test_forbidden_pagination_cannot_select_partial_feed_or_false_login(payload, health):
    from copy import deepcopy
    first = candidate_payload()
    first['data']['fetch__XDTUserDict']['clips_connection']['page_info'] = {'has_next_page':True, 'end_cursor':'cursor1'}
    second = deepcopy(first)
    second['data']['fetch__XDTUserDict']['clips_connection']['page_info']['end_cursor'] = 'cursor2'
    refused = response(payload, status=403)
    adapter = browser([refused])
    adapter.page = native_page(first, next_payload=second)
    with pytest.raises(ReelBrowserError, match='^reel_pagination_unreadable$') as error:
        await adapter.read_reels('123')
    assert error.value.health_signal == health
    assert adapter.context.request.fetch.await_count == 1
    assert adapter.context.request.fetch.call_args.args[0].endswith('/graphql/query')
    refused.dispose.assert_awaited_once()


async def test_repeated_cursor_never_selects_partial_feed():
    p=candidate_payload()
    p['data']['fetch__XDTUserDict']['clips_connection']['page_info']={'has_next_page':True,'end_cursor':'same'}
    adapter=browser([response(p)])
    adapter.page=native_page(p,next_payload=p)
    with pytest.raises(ReelBrowserError,match='reel_pagination_changed'):
        await adapter.read_reels('123')
    assert adapter.context.request.fetch.await_count==1


async def test_send_waits_for_mounted_composer_to_become_visible(monkeypatch):
    adapter = browser([
        response({'status': 'ok', 'viewer': {'pk': '123', 'username': 'sender'}}),
        response(thread()),
    ])
    adapter.sender_id, adapter.recipient_id = '123', '456'
    adapter.sender_handle, adapter.recipient_handle = 'sender', 'recipient'
    adapter.page = _MessagePage()
    adapter.page.adapter = adapter
    monkeypatch.setattr(adapter, '_inspect_profile', AsyncMock(return_value={
        'owner_id': '456', 'handle': 'recipient', 'following': True,
    }))
    locate = adapter.page.locator
    composer = _MessageLocator(adapter.page)
    composer.is_visible = AsyncMock(side_effect=[False, True])
    adapter.page.locator = lambda selector: composer if selector.startswith('textarea') else locate(selector)
    before = AsyncMock(return_value=True)
    outcome, evidence = await adapter.send(
        sender_id='123', recipient_id='456', reel_url=URL, body=BODY,
        before_mutation=before,
    )
    assert outcome == 'sent'
    assert evidence['server_confirmed'] is True
    assert composer.is_visible.await_count == 2
    before.assert_awaited_once()
    assert adapter.page.body_count == 1


@pytest.mark.parametrize('count,visible', [(0, False), (1, False), (2, True)])
async def test_control_readiness_times_out_without_unique_visible_control(count, visible):
    from engagemate.worker.follow_reel_reader import _one_visible
    locator = SimpleNamespace(count=AsyncMock(return_value=count), is_visible=AsyncMock(return_value=visible))
    assert await _one_visible(locator, timeout=0.002) is False


async def test_control_readiness_propagates_cancellation():
    from engagemate.worker.follow_reel_reader import _one_visible
    locator = SimpleNamespace(count=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await _one_visible(locator, timeout=0.002)
