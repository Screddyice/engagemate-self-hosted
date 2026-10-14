"""PostgreSQL connection-pool construction."""

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def create_pool(
    database_url: str,
    *,
    min_size: int = 1,
    max_size: int = 10,
    timeout: float = 10.0,
) -> ConnectionPool:
    """Create an opened pool whose query rows are dictionaries."""

    pool = ConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        check=ConnectionPool.check_connection,
        kwargs={"row_factory": dict_row},
        open=True,
    )
    pool.wait(timeout=timeout)
    return pool
