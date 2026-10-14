from contextlib import nullcontext
from unittest.mock import Mock
import pytest
from engagemate.browser.lease import browser_lease

def pool_fixture():
    connection = Mock()
    connection.transaction.return_value = nullcontext()
    connection.execute.return_value.fetchone.return_value = {'acquired': True}
    pool = Mock()
    pool.connection.return_value = nullcontext(connection)
    return pool, connection

def test_hosted_lease_timeout_is_bounded_and_transaction_local():
    pool, connection = pool_fixture()
    with browser_lease(pool, 'account', idle_timeout_seconds=720):
        pass
    assert connection.execute.call_args_list[0].args == (
        "SELECT set_config('idle_in_transaction_session_timeout', %s, true)", ('720s',),
    )

def test_unspecified_lease_keeps_existing_database_timeout():
    pool, connection = pool_fixture()
    with browser_lease(pool, 'account'):
        pass
    assert len(connection.execute.call_args_list) == 1
    assert 'pg_try_advisory_xact_lock' in connection.execute.call_args.args[0]

@pytest.mark.parametrize('timeout', [0, -1, 3901, True, '720'])
def test_invalid_timeout_never_opens_a_connection(timeout):
    pool, _ = pool_fixture()
    with pytest.raises(ValueError):
        with browser_lease(pool, 'account', idle_timeout_seconds=timeout):
            pass
    pool.connection.assert_not_called()


async def test_worker_factory_sets_bounded_transaction_local_timeout():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from engagemate.browser.lease import LeasedBrowserFactory

    pool, connection = pool_fixture()

    class Factory:
        @asynccontextmanager
        async def open(self, context):
            yield 'browser'

    async with LeasedBrowserFactory(pool, Factory()).open(SimpleNamespace(ig_account_id='account')) as browser:
        assert browser == 'browser'
    assert connection.execute.call_args_list[0].args == (
        "SELECT set_config('idle_in_transaction_session_timeout', %s, true)", ('3900s',),
    )
