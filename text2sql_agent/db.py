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
    ATHENA_ENDPOINT_URL,
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


def _is_identifier_char(char: str) -> bool:
    return char == "_" or char.isalnum()


def _quote_athena_non_ascii_identifiers(sql: str) -> str:
    """Quote Korean/non-ASCII identifiers for Athena/Trino.

    PostgreSQL accepts identifiers like 기준년월 without quotes, but Athena/Trino
    requires delimited identifiers ("기준년월"). This scanner leaves string
    literals, comments, and already quoted identifiers unchanged.
    """
    out: list[str] = []
    i = 0
    while i < len(sql):
        char = sql[i]

        if char == "'":
            start = i
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    i += 1
                    if i < len(sql) and sql[i] == "'":
                        i += 1
                        continue
                    break
                i += 1
            out.append(sql[start:i])
            continue

        if char == '"':
            start = i
            i += 1
            while i < len(sql):
                if sql[i] == '"':
                    i += 1
                    if i < len(sql) and sql[i] == '"':
                        i += 1
                        continue
                    break
                i += 1
            out.append(sql[start:i])
            continue

        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            start = i
            i = sql.find("\n", i)
            if i == -1:
                out.append(sql[start:])
                break
            out.append(sql[start:i])
            continue

        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            start = i
            end = sql.find("*/", i + 2)
            if end == -1:
                out.append(sql[start:])
                break
            i = end + 2
            out.append(sql[start:i])
            continue

        if _is_identifier_char(char):
            start = i
            i += 1
            while i < len(sql) and _is_identifier_char(sql[i]):
                i += 1
            token = sql[start:i]
            if any(ord(ch) > 127 for ch in token):
                out.append(f'"{token}"')
            else:
                out.append(token)
            continue

        out.append(char)
        i += 1

    return "".join(out)


def _normalize_common_sql_typos(sql: str) -> str:
    """Normalize a few keyboard/IME artifacts that break otherwise valid SQL."""
    out: list[str] = []
    i = 0
    while i < len(sql):
        char = sql[i]

        if char == "'":
            start = i
            i += 1
            while i < len(sql):
                if sql[i] == "'":
                    i += 1
                    if i < len(sql) and sql[i] == "'":
                        i += 1
                        continue
                    break
                i += 1
            out.append(sql[start:i])
            continue

        if char == '"':
            start = i
            i += 1
            while i < len(sql):
                if sql[i] == '"':
                    i += 1
                    if i < len(sql) and sql[i] == '"':
                        i += 1
                        continue
                    break
                i += 1
            out.append(sql[start:i])
            continue

        if char == "-" and i + 1 < len(sql) and sql[i + 1] == "-":
            start = i
            i = sql.find("\n", i)
            if i == -1:
                out.append(sql[start:])
                break
            out.append(sql[start:i])
            continue

        if char == "/" and i + 1 < len(sql) and sql[i + 1] == "*":
            start = i
            end = sql.find("*/", i + 2)
            if end == -1:
                out.append(sql[start:])
                break
            i = end + 2
            out.append(sql[start:i])
            continue

        # Korean IME can turn an intended one-letter alias reference `i.foo`
        # into `ㅣ.foo`. This is never a meaningful unquoted SQL alias here.
        if char == "ㅣ":
            j = i + 1
            while j < len(sql) and sql[j].isspace():
                j += 1
            if j < len(sql) and sql[j] == ".":
                out.append("i.")
                i = j + 1
                continue

        out.append(char)
        i += 1

    return "".join(out)


def _normalize_athena_ilike(sql: str) -> str:
    """Translate simple PostgreSQL ILIKE predicates to Athena-compatible SQL."""
    ident = r'(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:"[^"]+"|[A-Za-z_가-힣][A-Za-z0-9_가-힣]*)'
    string_literal = r"'(?:''|[^'])*'"
    pattern = re.compile(
        rf"(?P<lhs>{ident})\s+(?P<not>NOT\s+)?ILIKE\s+(?P<rhs>{string_literal})",
        re.IGNORECASE,
    )

    def repl(match: re.Match) -> str:
        op = "NOT LIKE" if match.group("not") else "LIKE"
        return f"LOWER({match.group('lhs')}) {op} LOWER({match.group('rhs')})"

    return pattern.sub(repl, sql)


def prepare_sql_for_backend(sql: str) -> str:
    """Apply backend-specific SQL normalization before validation/execution."""
    sql = _normalize_common_sql_typos(sql)
    if DB_BACKEND == "athena":
        sql = _normalize_athena_ilike(sql)
        return _quote_athena_non_ascii_identifiers(sql)
    return sql


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

    # 결과 위치는 (a) s3_staging_dir 직접 지정 또는 (b) 워크그룹의 managed/지정 결과 위치
    # 둘 중 하나가 있어야 한다. 둘 다 없으면 쿼리가 실패하므로 미리 막는다.
    if not ATHENA_S3_STAGING_DIR and not ATHENA_WORKGROUP:
        raise RuntimeError(
            "Athena 쿼리 결과 위치가 없습니다. ATHENA_S3_STAGING_DIR(s3:// 경로)를 지정하거나, "
            "결과 위치가 설정된 ATHENA_WORKGROUP을 지정하세요."
        )

    kwargs = {
        "region_name": ATHENA_REGION,
        "schema_name": ATHENA_DATABASE,
        "catalog_name": ATHENA_CATALOG,
    }
    if ATHENA_WORKGROUP:
        kwargs["work_group"] = ATHENA_WORKGROUP
    # staging이 지정되면 전달, 없으면 워크그룹 결과 위치를 쓰도록 빈 문자열로 명시
    # (pyathena가 AWS_ATHENA_S3_STAGING_DIR 환경변수로 폴백하는 것을 막는다).
    kwargs["s3_staging_dir"] = ATHENA_S3_STAGING_DIR or ""
    if ATHENA_PROFILE:
        kwargs["profile_name"] = ATHENA_PROFILE
    # VPC 엔드포인트/사내 프록시 등 커스텀 endpoint. pyathena가 boto3 athena client로 전달한다.
    if ATHENA_ENDPOINT_URL:
        kwargs["endpoint_url"] = ATHENA_ENDPOINT_URL
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
    execution_sql = prepare_sql_for_backend(stripped)
    first_token = execution_sql.split(None, 1)[0].upper() if execution_sql.split() else ""
    if first_token not in {"SELECT", "WITH"}:
        message = f"읽기 전용 SELECT/WITH 쿼리만 실행할 수 있습니다. (감지: {first_token})"
        _log_db_query(started, status="ERROR", result_count=0, error_message=message)
        return [], [], message
    if _DANGEROUS_SQL_RE.search(execution_sql):
        match = _DANGEROUS_SQL_RE.search(execution_sql)
        if match and match.start() < len(execution_sql) * 0.1:
            message = f"안전하지 않은 SQL 명령({match.group()})은 실행할 수 없습니다."
            _log_db_query(started, status="ERROR", result_count=0, error_message=message)
            return [], [], message
    try:
        if DB_BACKEND == "athena":
            columns, rows = _execute_athena(execution_sql)
        else:
            columns, rows = _execute_postgres(execution_sql)
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
