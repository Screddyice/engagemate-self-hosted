"""Opt-in Chrome check against intercepted Instagram pages; no real credentials."""
import os

import pytest
from engagemate.hosted_login.browser import LoginBrowser
from engagemate.hosted_login.protocol import ConnectError, SignIn, SubmitCode
from engagemate.proxy.base import Geo


@pytest.mark.skipif(os.environ.get('HOSTED_BROWSER_SMOKE') != '1', reason='opt-in local Chrome fixture')
@pytest.mark.asyncio
async def test_private_browser_follows_login_and_code_steps():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, executable_path=os.environ.get('HOSTED_BROWSER_EXECUTABLE') or None,
        )
        context = await browser.new_context(viewport={'width': 1024, 'height': 768})
        page = await context.new_page()
        async def instagram_fixture(route):
            if route.request.url == 'https://www.instagram.com/':
                html = """<html><body>Home<script type="application/json">
                    {"require":[["ScheduledServerJS","handle",null,[{"define":[
                    ["PolarisViewer",[],{"id":"123","data":{"id":"123","username":"fixture"}},1]
                    ]}]]]}</script></body></html>"""
            elif '/accounts/onetap/' in route.request.url:
                html = """<html><body><button>Save info</button>
                    <div role="button" onclick="location.href='/'">Not now</div></body></html>"""
            elif '/challenge/whatsapp/' in route.request.url:
                html = '''<html><body><p>Enter the code we sent via WhatsApp</p>
                    <input name="verificationCode" autocomplete="one-time-code">
                    <button onclick="document.body.dataset.smsRequested='yes'; document.querySelector('p').textContent='Enter the code we sent by text message'; this.remove()">Send code via SMS instead</button>
                    <button>backup codes</button></body></html>'''
            elif '/challenge/send/' in route.request.url:
                html = '''<html><body><p>Choose a verification method</p>
                    <button onclick="document.body.dataset.method='email'">Email</button>
                    <button onclick="document.body.dataset.requested='yes'; document.querySelector('p').textContent='Enter the code we sent by email'; this.remove(); document.querySelector('input').hidden=false">Send code</button>
                    <input name="verificationCode" autocomplete="one-time-code" hidden>
                    </body></html>'''
            elif '/challenge/continue/' in route.request.url:
                html = '''<html><body><p>Choose a verification method</p>
                    <button onclick="document.body.dataset.method='sms'">Text message</button>
                    <button onclick="location.href='/challenge/send/'">Continue</button></body></html>'''
            elif '/challenge/method/' in route.request.url:
                html = '''<html><body><p>Choose a verification method</p>
                    <button onclick="document.body.dataset.method='sms'">Text message</button>
                    <button>Authentication app</button></body></html>'''
            elif '/challenge/approval/' in route.request.url:
                html = '''<html><body><p>Check your phone to approve this login</p>
                    <button onclick="document.body.dataset.approved='yes'">This was me</button></body></html>'''
            elif '/challenge/unknown/' in route.request.url:
                html = '<html><body><p>Upload a video selfie</p></body></html>'
            elif '/challenge/' in route.request.url:
                html = '''<html><body><p>Enter the code sent by SMS</p>
                    <input name="verificationCode" autocomplete="one-time-code">
                    <button type="submit" onclick="document.body.dataset.submitted='yes'">Confirm</button>
                    <div role="link" onclick="document.body.dataset.resent='yes'">Get a new code</div></body></html>'''
            else:
                html = '''<html><body><input name="email"><input name="pass" type="password">
                    <button onclick="location.href='/challenge/'">Log In</button></body></html>'''
            await route.fulfill(status=200, content_type='text/html', body=html)
        await page.route('https://www.instagram.com/**', instagram_fixture)
        try:
            await page.goto('https://www.instagram.com/accounts/login/')
            remote = LoginBrowser(browser, context, page, Geo('US', 'NY', 'New York'))
            assert (await remote.step())['kind'] == 'login'
            await remote.input(SignIn(type='sign_in', username='fixture', password='fixture-password'))
            await page.wait_for_url('**/challenge/')
            assert (await remote.step())['kind'] == 'code'
            assert 'resend_code' in (await remote.step())['options']
            await remote.choose_action('resend_code')
            assert await page.locator('body').get_attribute('data-resent') == 'yes'
            await remote.input(SubmitCode(type='submit_code', code='123456'))
            assert await page.locator('input[name="verificationCode"]').input_value() == '123456'
            assert await page.locator('body').get_attribute('data-submitted') == 'yes'
            await page.goto('https://www.instagram.com/challenge/method/')
            assert (await remote.step())['kind'] == 'method'
            await remote.choose_action('use_sms')
            assert await page.locator('body').get_attribute('data-method') == 'sms'
            await page.goto('https://www.instagram.com/challenge/continue/')
            await remote.choose_action('use_sms')
            assert (await remote.step())['kind'] == 'method'
            await remote.choose_action('continue_verification')
            await page.wait_for_url('**/challenge/send/')
            assert 'send_code' in (await remote.step())['options']
            await remote.choose_action('use_email')
            assert await page.locator('body').get_attribute('data-method') == 'email'
            assert await page.locator('body').get_attribute('data-requested') is None
            await remote.choose_action('send_code')
            assert await page.locator('body').get_attribute('data-requested') == 'yes'
            step = await remote.step()
            assert step['kind'] == 'code' and step['channel'] == 'email'
            assert 'continue_verification' not in step['options']
            await page.goto('https://www.instagram.com/challenge/whatsapp/')
            step = await remote.step()
            assert step['channel'] == 'whatsapp'
            assert 'use_sms' in step['options']
            assert 'use_backup_code' in step['options']
            await remote.choose_action('use_sms')
            assert await page.locator('body').get_attribute('data-sms-requested') == 'yes'
            assert (await remote.step())['channel'] == 'sms'
            await page.goto('https://www.instagram.com/challenge/approval/')
            assert (await remote.step())['kind'] == 'approval'
            await remote.choose_action('confirm_login')
            assert await page.locator('body').get_attribute('data-approved') == 'yes'
            await page.goto('https://www.instagram.com/challenge/unknown/')
            assert (await remote.step())['kind'] == 'unsupported'
            await context.add_cookies([
                dict(name='sessionid', value='fixture-secret', domain='.instagram.com', path='/', secure=True, httpOnly=True, sameSite='None'),
                dict(name='ds_user_id', value='123', domain='.instagram.com', path='/', secure=True, httpOnly=False, sameSite='None'),
            ])
            await page.goto('https://www.instagram.com/accounts/onetap/')
            captured = await remote.capture('fixture')
            assert captured.username == 'fixture' and captured.ig_user_id == '123'
            assert page.url == 'https://www.instagram.com/'
            captured.cookies.clear()
            with pytest.raises(ConnectError) as error:
                await remote.capture('different_account')
            assert error.value.code == 'identity_mismatch'
            await remote.close()
            assert not browser.is_connected()
        finally:
            await browser.close()
