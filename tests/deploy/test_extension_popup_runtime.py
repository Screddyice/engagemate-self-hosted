"""Exercise popup recovery after an Instagram connection cannot authenticate."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def render_status(status):
    harness = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = {};
globalThis.document = { getElementById: (id) => nodes[id] ||= {
  textContent: '', disabled: false,
  classList: { add() {}, remove() {}, toggle() {} },
  addEventListener() {},
} };
globalThis.chrome = { runtime: { sendMessage() {} } };
vm.runInThisContext(fs.readFileSync('extension/popup.js', 'utf8'));
applyStatus(STATUS);
console.log(JSON.stringify({
  disabled: nodes['connect-btn'].disabled,
  button: nodes['connect-btn'].textContent,
  message: nodes['status-msg'].textContent,
}));
'''.replace('STATUS', json.dumps(status))
    result = subprocess.run(['node', '-'], input=harness, text=True,
                            cwd=ROOT, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('authenticated,error,expected', [
    (False, None, 'Sign in to EngageMate'),
    (False, 'auth_required', 'Sign in to EngageMate'),
    (True, 'auth_required', 'Sign in to EngageMate'),
    (True, 'network_error', 'Check your network'),
])
def test_failed_connection_stays_retryable(authenticated, error, expected):
    result = render_status({
        'auth': {'authenticated': authenticated},
        'instagram': {'loggedIn': True},
        'connectionRequested': True,
        'lastErrorCode': error,
    })
    assert result['disabled'] is False
    assert result['button'] == 'Connect Instagram'
    assert expected in result['message']


def test_pending_authenticated_connection_keeps_progress_indicator():
    result = render_status({
        'auth': {'authenticated': True}, 'instagram': {'loggedIn': True},
        'connectionRequested': True,
    })
    assert result['disabled'] is True
    assert result['button'] == 'Connecting…'


def test_safari_promise_messaging_renders_status_once():
    """Safari's `browser.runtime.sendMessage` may answer with a promise only."""

    harness = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = {};
globalThis.document = { getElementById: (id) => nodes[id] ||= {
  textContent: '', disabled: false,
  classList: { add() {}, remove() {}, toggle() {} },
  addEventListener() {},
} };
let chromeCalls = 0;
let safariCalls = 0;
globalThis.chrome = { runtime: { sendMessage() { chromeCalls += 1; } } };
globalThis.browser = { runtime: { sendMessage: async () => {
  safariCalls += 1;
  return {
    auth: { authenticated: true }, instagram: { loggedIn: true },
    connectionRequested: true, account: { id: 'account' },
  };
} } };
vm.runInThisContext(fs.readFileSync('extension/popup.js', 'utf8'));
setTimeout(() => console.log(JSON.stringify({
  chromeCalls, safariCalls,
  button: nodes['connect-btn'].textContent,
  status: nodes['ig-status'].textContent,
})), 0);
'''
    result = json.loads(subprocess.run(['node', '-'], input=harness, text=True,
                                       cwd=ROOT, capture_output=True, check=True).stdout)
    assert result == {
        'chromeCalls': 0,
        'safariCalls': 1,
        'button': 'Reconnect Instagram',
        'status': 'Connected',
    }


def test_connected_account_can_reconnect():
    result = render_status({
        'auth': {'authenticated': True}, 'instagram': {'loggedIn': True},
        'connectionRequested': True, 'account': {'id': 'account'},
    })
    assert result['disabled'] is False
    assert result['button'] == 'Reconnect Instagram'
