"""PostgreSQL connection pool and guarded SELECT execution."""

import os
import re
import time

import psycopg2
import psycopg2.pool
import sqlparse

from .common_services import emit_module_event


_DANGEROUS_SQL_RE = re.compile(
    r"(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)",
    re.IGNORECASE,
)
_MAX_ROWS = 500
_STATEMENT_TIMEOUT_MS = 30_000

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=int(os.getenv("DB_POOL_MAX", "10")),
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER", os.getenv("USER", "postgres")),
            password=os.getenv("DB_PASSWORD", ""),
        )
    return _pool


def get_db_connection():
    return _get_pool().getconn()


def _return_connection(conn):
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


def execute_sql(sql: str) -> tuple[list[str], list[tuple], str | None]:
    started = time.monotonic()
    stripped = sqlparse.format(sql, strip_comments=True).strip()
    first_token = stripped.split(None, 1)[0].upper() if stripped.split() else ""
    if first_token not in {"SELECT", "WITH"}:
        _log_db_query(
            started,
            status="ERROR",
            result_count=0,
            error_message=f"읽기 전용 SELECT/WITH 쿼리만 실행할 수 있습니다. (감지: {first_token})",
        )
        return [], [], f"읽기 전용 SELECT/WITH 쿼리만 실행할 수 있습니다. (감지: {first_token})"
    if _DANGEROUS_SQL_RE.search(stripped):
        match = _DANGEROUS_SQL_RE.search(stripped)
        if match and match.start() < len(stripped) * 0.1:
            _log_db_query(
                started,
                status="ERROR",
                result_count=0,
                error_message=f"안전하지 않은 SQL 명령({match.group()})은 실행할 수 없습니다.",
            )
            return [], [], f"안전하지 않은 SQL 명령({match.group()})은 실행할 수 없습니다."
    conn = None
    try:
        conn = get_db_connection()
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchmany(_MAX_ROWS) if cur.description else []
        cur.close()
        _log_db_query(started, status="SUCCESS", result_count=len(rows))
        return columns, rows, None
    except Exception as e:
        _log_db_query(started, status="ERROR", result_count=0, error_message=str(e))
        return [], [], str(e)
    finally:
        if conn:
            _return_connection(conn)


def _log_db_query(
    started: float,
    *,
    status: str,
    result_count: int,
    error_message: str | None = None,
) -> None:
    emit_module_event(
        module="db",
        event_type="db_query",
        status=status,
        latency_ms=int((time.monotonic() - started) * 1000),
        action="read",
        target={
            "system": "postgres",
            "schema": "card_system",
            "database": os.getenv("DB_NAME", "postgres"),
        },
        data_scope={
            "query_type": "select",
            "result_count": result_count,
            "sql_logged": False,
            "pii_masked": True,
        },
        store_provider="postgres",
        metadata={
            "query_type": "select",
            "result_count": result_count,
            "max_rows": _MAX_ROWS,
            "statement_timeout_ms": _STATEMENT_TIMEOUT_MS,
        },
        error_type="DatabaseQueryError" if error_message else None,
        error_message=error_message,
        log_level="ERROR" if status == "ERROR" else "INFO",
    )
