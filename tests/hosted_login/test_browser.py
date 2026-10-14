from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from engagemate.hosted_login.browser import LoginBrowser
from engagemate.hosted_login.protocol import ConnectError
from engagemate.proxy.base import Geo


@pytest.fixture
def browser():
    cookies = [
        dict(name='sessionid',value='fixture-secret',domain='.instagram.com',path='/',
             secure=True,httpOnly=True,sameSite='None',expires=-1),
        dict(name='ds_user_id',value='123',domain='.instagram.com',path='/',
             secure=True,httpOnly=False,sameSite='None',expires=-1),
    ]
    response=SimpleNamespace(status=200,json=AsyncMock(return_value={'user':{'pk':'123','username':'demo_user'}}),dispose=AsyncMock())
    context=SimpleNamespace(cookies=AsyncMock(side_effect=lambda *args: deepcopy(cookies)),request=SimpleNamespace(get=AsyncMock(return_value=response)))
    page=SimpleNamespace(url='https://www.instagram.com/',
        goto=AsyncMock(return_value=SimpleNamespace(status=200)),
        wait_for_function=AsyncMock(return_value=SimpleNamespace(json_value=AsyncMock(return_value=[{'id':'123','user_id':'123','username':'demo_user'}]),dispose=AsyncMock())),
        evaluate=AsyncMock(return_value='Mozilla/5.0 (X11; Linux x86_64) Chrome/145.0'))
    return LoginBrowser(SimpleNamespace(close=AsyncMock()),context,page,Geo('US','NY','New York'))


@pytest.mark.asyncio
async def test_capture_only_verified_matching_identity(browser):
    capture = await browser.capture('demo_user')
    assert capture.ig_user_id == '123'
    assert capture.username == 'demo_user'
    assert capture.cookies[0].value == 'fixture-secret'
    assert 'fixture-secret' not in repr(capture)
    browser.page.goto.assert_awaited_once()
    browser.page.wait_for_function.return_value.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_mismatched_username_never_connects(browser):
    with pytest.raises(ConnectError) as error:
        await browser.capture('somebody_else')
    assert error.value.code == 'identity_mismatch'


@pytest.mark.asyncio
async def test_numeric_identity_must_match_cookie(browser):
    browser.page.wait_for_function.return_value.json_value.return_value=[{'id':'456','user_id':'456','username':'demo_user'}]
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'identity_unavailable'


@pytest.mark.asyncio
async def test_challenge_does_not_become_connected(browser):
    browser.page.url='https://www.instagram.com/challenge/'
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'sign_in_required'
    browser.context.request.get.assert_not_called()


@pytest.mark.asyncio
async def test_rate_limit_is_not_retried(browser):
    browser.page.goto.return_value.status=429
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'instagram_rate_limited'
    browser.page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_sign_in_fills_visible_instagram_form_once(browser):
    browser.page.url = 'https://www.instagram.com/accounts/login/'
    chooser = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), click=AsyncMock())
    submit = SimpleNamespace(count=AsyncMock(return_value=1), is_enabled=AsyncMock(return_value=True), click=AsyncMock())
    user = SimpleNamespace(count=AsyncMock(return_value=1), fill=AsyncMock())
    password = SimpleNamespace(count=AsyncMock(return_value=1), fill=AsyncMock())
    browser.page.get_by_role = Mock(side_effect=[chooser, submit])
    browser.page.locator = Mock(side_effect=[user, password])

    await browser.sign_in('demo_user', 'fixture-password')

    chooser.click.assert_awaited_once_with(timeout=5000)
    user.fill.assert_awaited_once_with('demo_user', timeout=5000)
    password.fill.assert_awaited_once_with('fixture-password', timeout=5000)
    submit.click.assert_awaited_once_with(timeout=5000, no_wait_after=True)


@pytest.mark.asyncio
async def test_sign_in_rejects_wrong_page_before_touching_fields(browser):
    browser.page.locator = Mock()
    with pytest.raises(ConnectError) as error:
        await browser.sign_in('demo_user', 'fixture-password')
    assert error.value.code == 'sign_in_required'
    browser.page.locator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('executable', [None, '/local/chrome-for-testing'])
async def test_browser_executable_override_preserves_proxy_and_context(monkeypatch, executable):
    from engagemate.hosted_login import browser as module
    geo = Geo('US', 'NY', 'New York')
    registry = Mock()
    registry.require.return_value = ('America/New_York', 'en-US')
    monkeypatch.setattr(module.FingerprintGeoRegistry, 'from_settings', Mock(return_value=registry))
    page = SimpleNamespace(goto=AsyncMock(), on=Mock())
    context = SimpleNamespace(
        set_default_timeout=Mock(), set_default_navigation_timeout=Mock(),
        route=AsyncMock(), route_web_socket=AsyncMock(), new_page=AsyncMock(return_value=page), on=Mock(),
    )
    instance = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    launch = AsyncMock(return_value=instance)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    settings = SimpleNamespace(hosted_login_browser_executable=executable)
    endpoint = SimpleNamespace(server='http://proxy.invalid:8080', username='fixture', password='fixture')
    remote = await LoginBrowser.open(playwright, SimpleNamespace(geo=geo, endpoint=endpoint), settings)
    kwargs = launch.call_args.kwargs
    assert kwargs['headless'] is True
    assert kwargs['proxy']['server'] == endpoint.server
    if executable:
        assert kwargs['executable_path'] == executable
        assert 'channel' not in kwargs
    else:
        assert kwargs['channel'] == 'chrome'
        assert 'executable_path' not in kwargs
    assert instance.new_context.call_args.kwargs['timezone_id'] == 'America/New_York'
    await remote.close()


@pytest.mark.parametrize('copy, expected', [
    ('Enter the code we sent to your WhatsApp.', 'whatsapp'),
    ('Enter the code from your authentication app.\nSend code via WhatsApp', 'authenticator'),
    ('Enter the code from your authenticator app.\nTry another way\nText message', 'authenticator'),
    ('Enter your security code\nWhatsApp\nEmail\nText message', 'unknown'),
    ('Enter the code sent by SMS', 'sms'),
    ('Enter the code sent to your email', 'email'),
    ('Enter a backup code', 'backup'),
    ('Enter the code from your email or WhatsApp', 'unknown'),
])
def test_code_delivery_uses_instructions_not_alternate_methods(copy, expected):
    from engagemate.hosted_login.browser import code_channel
    assert code_channel(copy) == expected


@pytest.mark.asyncio
async def test_navigation_without_auth_cookies_is_not_ready(browser):
    browser.context.cookies.side_effect = lambda *args: []
    fields = SimpleNamespace(count=AsyncMock(return_value=0), all_text_contents=AsyncMock(return_value=[]), inner_text=AsyncMock(return_value='Please wait'))
    browser.page.locator = lambda selector: fields
    assert (await browser.step())['kind'] == 'unsupported'


@pytest.mark.asyncio
async def test_ready_requires_secure_authenticated_cookie_metadata(browser):
    assert (await browser.step())['kind'] == 'ready'


@pytest.mark.asyncio
async def test_identity_endpoint_failure_is_distinct_from_missing_sign_in(browser, caplog):
    browser.page.goto.return_value.status = 403
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'identity_check_unavailable'
    assert 'fixture-secret' not in caplog.text


@pytest.mark.asyncio
async def test_missing_session_metadata_has_specific_error(browser, caplog):
    browser.context.cookies.side_effect = lambda *args: []
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'session_unverified'
    browser.context.request.get.assert_not_called()
    assert 'fixture-secret' not in caplog.text


@pytest.mark.asyncio
async def test_capture_timeout_keeps_browser_available_and_redacts_exception(browser, caplog):
    browser.page.goto.side_effect = TimeoutError('fixture-secret')
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'identity_check_unavailable'
    browser.browser.close.assert_not_called()
    assert 'TimeoutError' in caplog.text
    assert 'fixture-secret' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('viewers', [
    [], [{'id':'123','user_id':'999','username':'demo_user'}],
    [{'id':'123','username':'demo_user'}],
    [{'id':'123','user_id':'123','username':'other'}],
    [{'id':'123','user_id':'123','username':'demo_user'},
     {'id':'456','user_id':'456','username':'demo_user'}],
    {'id':'123','user_id':'123','username':'demo_user'},
])
async def test_unavailable_conflicting_or_other_viewer_cannot_connect(browser, viewers):
    browser.page.wait_for_function.return_value.json_value.return_value = viewers
    with pytest.raises(ConnectError):
        await browser.capture('demo_user')


@pytest.mark.asyncio
async def test_redirect_to_login_cannot_connect(browser):
    async def redirect(*args, **kwargs):
        browser.page.url = 'https://www.instagram.com/accounts/login/'
        return SimpleNamespace(status=200)
    browser.page.goto.side_effect = redirect
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'sign_in_required'
    browser.page.wait_for_function.assert_not_called()


@pytest.mark.asyncio
async def test_cookie_identity_changed_during_verification_cannot_connect(browser):
    original = browser.context.cookies.side_effect()
    changed = deepcopy(original)
    changed[1]['value'] = '456'
    browser.context.cookies.side_effect = [original, changed]
    with pytest.raises(ConnectError) as error:
        await browser.capture('demo_user')
    assert error.value.code == 'session_unverified'
    assert original == []


@pytest.mark.asyncio
async def test_onetap_skips_save_info_before_verifying(browser):
    browser.page.url = 'https://www.instagram.com/accounts/onetap/'
    dismiss = SimpleNamespace(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), click=AsyncMock())
    browser.page.get_by_role = Mock(return_value=dismiss)
    async def leave(*args, **kwargs):
        browser.page.url = 'https://www.instagram.com/'
    browser.page.wait_for_url = AsyncMock(side_effect=leave)
    assert (await browser.capture('demo_user')).username == 'demo_user'
    browser.page.get_by_role.assert_called_once_with('button', name='Not now', exact=True)
    dismiss.click.assert_awaited_once()
