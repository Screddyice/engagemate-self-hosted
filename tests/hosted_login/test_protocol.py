import pytest
from pydantic import TypeAdapter, ValidationError

from engagemate.hosted_login.protocol import Command, allowed_destination


@pytest.mark.parametrize('url', [
    'https://www.instagram.com/accounts/login/',
    'https://instagram.com/challenge/', 'https://i.instagram.com:443/api/',
])
def test_allows_instagram(url):
    assert allowed_destination(url, navigation=True)


@pytest.mark.parametrize('url', [
    'http://www.instagram.com/', 'https://instagram.com.evil.test/',
    'https://evilinstagram.com/', 'https://instagram.com@127.0.0.1/',
    'https://user:password@instagram.com/', 'https://instagram.com:8443/',
    'https://localhost/', 'http://169.254.169.254/latest/meta-data/',
    'file:///etc/passwd', 'data:text/html,hello', 'https://[::1]/',
    'https://instagram.com:invalid/', 'https://instagram.com./',
])
def test_rejects_other_destinations(url):
    assert not allowed_destination(url)


def test_assets_cannot_be_top_level_navigation():
    assert allowed_destination('https://s.cdninstagram.com/a.js')
    assert not allowed_destination('https://s.cdninstagram.com/a.js', navigation=True)
    assert allowed_destination('https://www.google.com/recaptcha/api.js')
    assert not allowed_destination('https://www.google.com/search')


@pytest.mark.parametrize('body', [
    {'type': 'click', 'x': 1, 'y': 1},
    {'type': 'text', 'text': 'private'},
    {'type': 'key', 'key': 'Enter'},
    {'type': 'scroll', 'delta_y': 100},
    {'type': 'navigate', 'url': 'https://instagram.com'},
    {'type': 'click', 'x': 1, 'y': 1, 'user_id': 'forged'},
    {'type': 'sign_in', 'username': 'demo_user', 'password': ''},
    {'type': 'sign_in', 'username': 'DEMO_user', 'password': 'fixture'},
    {'type': 'sign_in', 'username': 'demo_user', 'password': 'line\nbreak'},
    {'type': 'sign_in', 'username': 'demo_user', 'password': 'fixture', 'url': 'https://example.com'},
    {'type': 'submit_code', 'code': '1\n234'},
    {'type': 'submit_code', 'code': '123'},
    {'type': 'choose_action', 'action': 'click_anything'},
])
def test_only_bounded_commands(body):
    with pytest.raises(ValidationError):
        TypeAdapter(Command).validate_python(body)


def test_input_repr_redacts_secrets():
    sign_in = TypeAdapter(Command).validate_python({
        'type': 'sign_in', 'username': 'demo_user', 'password': 'fixture-password',
    })
    assert 'fixture-password' not in repr(sign_in)
    code = TypeAdapter(Command).validate_python({'type': 'submit_code', 'code': '123456'})
    assert '123456' not in repr(code)
