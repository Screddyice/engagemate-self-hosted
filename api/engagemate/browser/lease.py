"""Cross-process exclusion for browsers using the same Instagram session."""

from contextlib import asynccontextmanager, contextmanager


# Maximum configured worker run (3600s) plus five minutes for browser cleanup.
WORKER_BROWSER_LEASE_TIMEOUT_SECONDS = 3900


class BrowserBusy(RuntimeError):
    pass


@contextmanager
def browser_lease(pool, account_id, *, profile_capture=False, idle_timeout_seconds=None):
    # Transaction-scoped locks release on cancellation and cannot leak into a
    # pooled connection. No account rows stay locked during browser I/O.
    if idle_timeout_seconds is not None and (
        type(idle_timeout_seconds) is not int or not 1 <= idle_timeout_seconds <= WORKER_BROWSER_LEASE_TIMEOUT_SECONDS
    ):
        raise ValueError("invalid browser lease timeout")
    with pool.connection() as connection, connection.transaction():
        if idle_timeout_seconds is not None:
            # Human sign-in and worker runs can exceed PostgreSQL's idle limit.
            # Keep the override bounded and local to this lock transaction.
            connection.execute(
                "SELECT set_config('idle_in_transaction_session_timeout', %s, true)",
                (f"{idle_timeout_seconds}s",),
            )
        keys = [f"engagemate-browser:{account_id}"]
        if profile_capture:
            keys.insert(0, "engagemate-profile-capture")
        for key in keys:
            row = connection.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired",
                (key,),
            ).fetchone()
            acquired = row["acquired"] if isinstance(row, dict) else row[0]
            if acquired is not True:
                raise BrowserBusy("account_browser_busy")
        yield


class LeasedBrowserFactory:
    def __init__(self, pool, factory):
        self.pool, self.factory = pool, factory

    @asynccontextmanager
    async def open(self, context):
        with browser_lease(
            self.pool, context.ig_account_id,
            idle_timeout_seconds=WORKER_BROWSER_LEASE_TIMEOUT_SECONDS,
        ):
            async with self.factory.open(context) as browser:
                yield browser
