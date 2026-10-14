from unittest.mock import MagicMock
import pytest
from deploy.healthcheck import probe_hosted_login_schema


def test_disabled_hosted_rollout_does_not_touch_database():
    connect = MagicMock(side_effect=AssertionError('must not connect'))
    probe_hosted_login_schema({}, connect=connect)
    connect.assert_not_called()


def test_enabled_requires_table_and_unique_reservation_indexes():
    connect = MagicMock()
    cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = {'n':2}
    env = {'HOSTED_LOGIN_ENABLED':'true', 'DATABASE_URL':'postgresql://fixture'}
    probe_hosted_login_schema(env, connect=connect)
    cursor.fetchone.return_value = {'n':1}
    with pytest.raises(ValueError, match='migration 0025'):
        probe_hosted_login_schema(env, connect=connect)
    cursor.execute.side_effect = RuntimeError('private-credential')
    with pytest.raises(ValueError) as error:
        probe_hosted_login_schema(env, connect=connect)
    assert 'private-credential' not in str(error.value)
