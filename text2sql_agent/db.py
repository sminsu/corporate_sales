"""Read-only SQL execution with a pluggable backend (PostgreSQL or Amazon Athena).

The rest of the app depends on a single function, ``execute_sql``, which returns
``(columns, rows, error)``. The validation guards (read-only SELECT/WITH only) are
shared across backends; only the actual execution differs and is selected by the
``DB_BACKEND`` config ("postgres" or "athena").
"""

import re
import time

import sqlparse

from .common_services import emit_module_event
from .config import (
    ATHENA_CATALOG,
    ATHENA_DATABASE,
    ATHENA_PROFILE,
    ATHENA_REGION,
    ATHENA_S3_STAGING_DIR,
    ATHENA_WORKGROUP,
    DB_BACKEND,
    DB_DSN,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_POOL_MAX,
    DB_PORT,
    DB_USER,
)


_DANGEROUS_SQL_RE = re.compile(
    r"(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)",
    re.IGNORECASE,
)
_MAX_ROWS = 500
_STATEMENT_TIMEOUT_MS = 30_000


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------
_pool = None


def _get_pool():
    global _pool
    import psycopg2.pool  # 지연 import: athena 전용 배포에서 psycopg2 미설치를 허용.

    if _pool is None or _pool.closed:
        if DB_DSN:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, DB_POOL_MAX, DB_DSN)
        else:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=DB_POOL_MAX,
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD,
            )
    return _pool


def get_db_connection():
    return _get_pool().getconn()


def _return_connection(conn):
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


def _execute_postgres(sql: str) -> tuple[list[str], list[tuple]]:
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
        return columns, rows
    finally:
        if conn:
            _return_connection(conn)


# ---------------------------------------------------------------------------
# Amazon Athena backend (pyathena, DB-API)
# ---------------------------------------------------------------------------
_athena_conn = None


def _get_athena_connection():
    """pyathena DB-API 커넥션을 1회 생성해 재사용한다.

    인증은 표준 AWS 자격증명 체인(환경변수/AWS_PROFILE/IAM 역할)을 따른다.
    """
    global _athena_conn
    if _athena_conn is not None:
        return _athena_conn

    from pyathena import connect  # 지연 import: postgres 전용 배포에서 pyathena 미설치 허용.

    if not ATHENA_S3_STAGING_DIR:
        raise RuntimeError(
            "ATHENA_S3_STAGING_DIR(또는 ATHENA_S3_OUTPUT)가 설정되지 않았습니다. "
            "Athena 쿼리 결과를 저장할 s3:// 경로가 필요합니다."
        )

    kwargs = {
        "region_name": ATHENA_REGION,
        "s3_staging_dir": ATHENA_S3_STAGING_DIR,
        "work_group": ATHENA_WORKGROUP,
        "schema_name": ATHENA_DATABASE,
        "catalog_name": ATHENA_CATALOG,
    }
    if ATHENA_PROFILE:
        kwargs["profile_name"] = ATHENA_PROFILE
    _athena_conn = connect(**kwargs)
    return _athena_conn


def _execute_athena(sql: str) -> tuple[list[str], list[tuple]]:
    conn = _get_athena_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchmany(_MAX_ROWS) if cur.description else []
        return columns, [tuple(row) for row in rows]
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Public API (backend-agnostic)
# ---------------------------------------------------------------------------
def execute_sql(sql: str) -> tuple[list[str], list[tuple], str | None]:
    started = time.monotonic()
    stripped = sqlparse.format(sql, strip_comments=True).strip()
    first_token = stripped.split(None, 1)[0].upper() if stripped.split() else ""
    if first_token not in {"SELECT", "WITH"}:
        message = f"읽기 전용 SELECT/WITH 쿼리만 실행할 수 있습니다. (감지: {first_token})"
        _log_db_query(started, status="ERROR", result_count=0, error_message=message)
        return [], [], message
    if _DANGEROUS_SQL_RE.search(stripped):
        match = _DANGEROUS_SQL_RE.search(stripped)
        if match and match.start() < len(stripped) * 0.1:
            message = f"안전하지 않은 SQL 명령({match.group()})은 실행할 수 없습니다."
            _log_db_query(started, status="ERROR", result_count=0, error_message=message)
            return [], [], message
    try:
        if DB_BACKEND == "athena":
            columns, rows = _execute_athena(sql)
        else:
            columns, rows = _execute_postgres(sql)
        _log_db_query(started, status="SUCCESS", result_count=len(rows))
        return columns, rows, None
    except Exception as e:
        _log_db_query(started, status="ERROR", result_count=0, error_message=str(e))
        return [], [], str(e)


def _log_db_query(
    started: float,
    *,
    status: str,
    result_count: int,
    error_message: str | None = None,
) -> None:
    is_athena = DB_BACKEND == "athena"
    emit_module_event(
        module="db",
        event_type="db_query",
        status=status,
        latency_ms=int((time.monotonic() - started) * 1000),
        action="read",
        target={
            "system": "athena" if is_athena else "postgres",
            "schema": "card_system",
            "database": ATHENA_DATABASE if is_athena else DB_NAME,
        },
        data_scope={
            "query_type": "select",
            "result_count": result_count,
            "sql_logged": False,
            "pii_masked": True,
        },
        store_provider="athena" if is_athena else "postgres",
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
