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
    DB_DSN_ERROR,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_POOL_MAX,
    DB_PORT,
    DB_SCHEMA,
    DB_USER,
)


_DANGEROUS_SQL_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"MERGE|REPLACE|UPSERT|COPY|CALL|DO|EXECUTE|VACUUM|ANALYZE|REFRESH|"
    r"LOCK|SET|RESET|INTO)\b",
    re.IGNORECASE,
)
_MAX_ROWS = 500
_STATEMENT_TIMEOUT_MS = 30_000


def _is_identifier_char(char: str) -> bool:
    return char == "_" or char.isalnum()


def _split_sql_segments(sql: str) -> list[tuple[bool, str]]:
    """Split SQL into ``(is_protected, text)`` segments.

    Protected segments — single-quoted string literals, double-quoted
    identifiers, line (``--``) comments and block (``/* */``) comments — must be
    passed through unchanged by any rewriter. Everything else is "code" that a
    rewriter may transform. Both SQL normalizers below share this single scan so
    the literal/comment-skipping logic lives in one place.
    """
    segments: list[tuple[bool, str]] = []
    code_start = 0
    i = 0
    n = len(sql)

    def flush_code(end: int) -> None:
        if end > code_start:
            segments.append((False, sql[code_start:end]))

    while i < n:
        char = sql[i]

        if char in ("'", '"'):
            flush_code(i)
            quote = char
            start = i
            i += 1
            while i < n:
                if sql[i] == quote:
                    i += 1
                    if i < n and sql[i] == quote:  # escaped quote ('' or "")
                        i += 1
                        continue
                    break
                i += 1
            segments.append((True, sql[start:i]))
            code_start = i
            continue

        if char == "-" and i + 1 < n and sql[i + 1] == "-":
            flush_code(i)
            newline = sql.find("\n", i)
            end = n if newline == -1 else newline
            segments.append((True, sql[i:end]))
            i = code_start = end
            continue

        if char == "/" and i + 1 < n and sql[i + 1] == "*":
            flush_code(i)
            close = sql.find("*/", i + 2)
            end = n if close == -1 else close + 2
            segments.append((True, sql[i:end]))
            i = code_start = end
            continue

        i += 1

    flush_code(n)
    return segments


def _validate_read_only_sql(sql: str) -> str | None:
    """Return a user-facing error when *sql* is not one read-only query.

    Merely checking the first token is insufficient: ``SELECT 1; DROP ...`` and
    data-modifying CTEs both start with an allowed token.  We therefore require a
    single statement and inspect every code segment while deliberately ignoring
    quoted literals, quoted identifiers, and comments.  This keeps values such as
    ``'DELETE'`` from becoming false positives without opening a CTE bypass.
    """
    stripped = sqlparse.format(sql or "", strip_comments=True).strip()
    statements = [statement.strip() for statement in sqlparse.split(stripped) if statement.strip().rstrip(";").strip()]
    if not statements:
        return "실행할 SQL이 없습니다."
    if len(statements) != 1:
        return "한 번에 하나의 읽기 전용 쿼리만 실행할 수 있습니다."

    statement = statements[0]
    first_token = statement.split(None, 1)[0].upper() if statement.split() else ""
    if first_token not in {"SELECT", "WITH"}:
        return f"읽기 전용 SELECT/WITH 쿼리만 실행할 수 있습니다. (감지: {first_token})"

    code_only = "".join(text for protected, text in _split_sql_segments(statement) if not protected)
    match = _DANGEROUS_SQL_RE.search(code_only)
    if match:
        return f"안전하지 않은 SQL 명령({match.group().upper()})은 실행할 수 없습니다."
    return None


def _quote_athena_non_ascii_identifiers(sql: str) -> str:
    """Quote Korean/non-ASCII identifiers for Athena/Trino.

    PostgreSQL accepts identifiers like 기준년월 without quotes, but Athena/Trino
    requires delimited identifiers ("기준년월"). This scanner leaves string
    literals, comments, and already quoted identifiers unchanged.
    """
    out: list[str] = []
    for protected, text in _split_sql_segments(sql):
        if protected:
            out.append(text)
            continue
        i = 0
        n = len(text)
        while i < n:
            char = text[i]
            if _is_identifier_char(char):
                start = i
                i += 1
                while i < n and _is_identifier_char(text[i]):
                    i += 1
                token = text[start:i]
                out.append(f'"{token}"' if any(ord(ch) > 127 for ch in token) else token)
            else:
                out.append(char)
                i += 1
    return "".join(out)


def _normalize_common_sql_typos(sql: str) -> str:
    """Normalize a few keyboard/IME artifacts that break otherwise valid SQL."""
    sql = sql.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    out: list[str] = []
    for protected, text in _split_sql_segments(sql):
        if protected:
            if re.fullmatch(r'"20\d{4}(?:\d{2})?"', text):
                out.append(f"'{text[1:-1]}'")
            else:
                out.append(text)
            continue
        text = re.sub(r"\bDISTINCT\s*\.\s*", "DISTINCT ", text, flags=re.IGNORECASE)
        text = re.sub(
            r"\b([A-Za-z][A-Za-z0-9]*)\s+(_[A-Za-z][A-Za-z0-9_]*)\s+AS\s*\(",
            r"\1\2 AS (",
            text,
            flags=re.IGNORECASE,
        )
        i = 0
        n = len(text)
        while i < n:
            char = text[i]
            # Korean IME can turn an intended one-letter alias reference `i.foo`
            # into `ㅣ.foo`. This is never a meaningful unquoted SQL alias here.
            if char == "ㅣ":
                j = i + 1
                while j < n and text[j].isspace():
                    j += 1
                if j < n and text[j] == ".":
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
        if DB_DSN_ERROR:
            raise RuntimeError("PostgreSQL 접속 정보를 Secrets Manager에서 불러오지 못했습니다.")
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


def _return_connection(conn, *, close: bool = False):
    try:
        _get_pool().putconn(conn, close=close)
    except TypeError:
        # Small test doubles and older pool implementations may not expose the
        # keyword.  A broken connection must never be returned for reuse.
        if close:
            try:
                conn.close()
            except Exception:
                pass
        else:
            _get_pool().putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def _execute_postgres(sql: str) -> tuple[list[str], list[tuple]]:
    conn = None
    cur = None
    discard_connection = False
    try:
        conn = get_db_connection()
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchmany(_MAX_ROWS) if cur.description else []
        return columns, rows
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                discard_connection = True
        if conn is not None:
            # End both successful SELECT transactions and failed/aborted ones
            # before returning the connection to the shared pool.
            try:
                conn.rollback()
            except Exception:
                discard_connection = True
            _return_connection(conn, close=discard_connection)


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
    safety_error = _validate_read_only_sql(execution_sql)
    if safety_error:
        _log_db_query(started, status="ERROR", result_count=0, error_message=safety_error)
        return [], [], safety_error
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
            "schema": DB_SCHEMA,
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
