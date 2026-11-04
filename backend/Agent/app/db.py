"""PostgreSQL connection pools for the agent.

The agent's database role can read and write the audit schema and create
tables in the langgraph schema. It has no privileges on the erp schema:
ERP data is only available through the mock-erp HTTP API.

The database is on Supabase, reached through its session pooler. Each
open connection holds a pooler slot, so the pools stay small, and each
connection is checked before use because remote ones can drop when idle.
"""
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .settings import settings

_pool: ConnectionPool | None = None
_checkpoint_pool: ConnectionPool | None = None


def init(timeout: float = 15) -> None:
    global _pool, _checkpoint_pool
    s = settings()
    _pool = ConnectionPool(s.conninfo(), min_size=1, max_size=6, check=ConnectionPool.check_connection,
                           kwargs={"row_factory": dict_row}, open=True)
    _pool.wait(timeout=timeout)
    # The LangGraph checkpointer needs autocommit and its own search_path,
    # so its tables land in the langgraph schema.
    _checkpoint_pool = ConnectionPool(
        s.conninfo(search_path="langgraph"), min_size=1, max_size=3, check=ConnectionPool.check_connection,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True)
    _checkpoint_pool.wait(timeout=timeout)


def connection():
    """Context manager: commits on success, rolls back on exception."""
    if _pool is None:
        raise RuntimeError("Database pool not initialised")
    return _pool.connection()


def checkpoint_pool() -> ConnectionPool:
    if _checkpoint_pool is None:
        raise RuntimeError("Checkpoint pool not initialised")
    return _checkpoint_pool


def ping() -> None:
    with connection() as conn:
        conn.execute("SELECT 1")


def close() -> None:
    for pool in (_pool, _checkpoint_pool):
        if pool is not None:
            pool.close()
