import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from engagemate.config import Settings
from engagemate.hosted_login.app import create_app


def config(enabled=True):
    return Settings(_env_file=None, database_url='postgresql://unused',
                    session_encryption_key=base64.urlsafe_b64encode(b'x'*32).decode(),
                    internal_api_key='internal-test-only-key', hosted_login_enabled=enabled)


@pytest.fixture
def client():
    service = SimpleNamespace(start=AsyncMock(return_value={'id': str(uuid4()), 'state':'active'}),
                              close=AsyncMock(), input=AsyncMock(),
                              step=AsyncMock(return_value={'kind':'code','channel':'sms','invalid':False,'options':[]}))
    with TestClient(create_app(settings=config(), service=service)) as client:
        client.service = service
        yield client


def headers():
    return {'X-Internal-Api-Key': 'internal-test-only-key', 'X-User-Id': str(uuid4())}


def test_requires_server_key_and_uuid(client):
    assert client.post('/v1/connect', json={'username':'demo_user','timezone':'UTC'}).status_code == 401
    assert client.post('/v1/connect', headers={**headers(),'X-User-Id':'bad'},
                       json={'username':'demo_user','timezone':'UTC'}).status_code == 401
    client.service.start.assert_not_called()


def test_does_not_echo_input_or_browser_errors(client):
    body={'type':'text', 'text':'private-password', 'unknown':'field'}
    result=client.post(f'/v1/connect/{uuid4()}/input', headers=headers(), json=body)
    assert result.status_code == 422
    assert result.json() == {'code':'invalid_input'}
    assert 'private-password' not in result.text
    client.service.start.side_effect=RuntimeError('private-password')
    result=client.post('/v1/connect', headers=headers(), json={'username':'demo_user','timezone':'UTC'})
    assert result.status_code == 503
    assert 'private-password' not in result.text
    assert result.headers['cache-control'] == 'no-store, private'


def test_body_limit_and_private_step(client):
    result=client.post(f'/v1/connect/{uuid4()}/input', headers=headers(), content=b'x'*8193)
    assert result.status_code == 413
    result=client.get(f'/v1/connect/{uuid4()}/step', headers=headers())
    assert result.json()['kind'] == 'code'
    assert client.get(f'/v1/connect/{uuid4()}/frame', headers=headers()).status_code == 404
    assert 'no-store' in result.headers['cache-control']


def test_disabled_service_has_no_browser_or_db_dependency():
    with TestClient(create_app(settings=config(False))) as client:
        assert client.get('/health').json() == {'status':'ok','enabled':False}
        assert client.post('/v1/connect',headers=headers(),
                           json={'username':'demo_user','timezone':'UTC'}).status_code == 503
