"""Read-only SQL execution with a pluggable backend (PostgreSQL or Amazon Athena).

The rest of the app depends on a single function, ``execute_sql``, which returns
``(columns, rows, error)``. The validation guards (read-only SELECT/WITH only) are
shared across backends; only the actual execution differs and is selected by the
``DB_BACKEND`` config ("postgres" or "athena").
"""

import re
import time
from numbers import Number

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
    DB_SCHEMA_PREFIX,
    DB_USER,
    MAX_QUERY_ROW_LIMIT,
)
from .time_policy import accumulation_policy_for


_DANGEROUS_SQL_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"MERGE|REPLACE|UPSERT|COPY|CALL|DO|EXECUTE|VACUUM|ANALYZE|REFRESH|"
    r"LOCK|SET|RESET|INTO)\b",
    re.IGNORECASE,
)
_COUNT_OUTPUT_RE = re.compile(
    r"^\s*(?:(?:COALESCE|CAST|TRY_CAST|ROUND)\s*\(\s*)*COUNT\s*\(",
    re.IGNORECASE,
)
_MAX_ROWS = 500
_STATEMENT_TIMEOUT_MS = 30_000
_TBD_ZERO_RESULT_ATTEMPTS = 3
_TABLE_REFERENCE_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+'
    r'(?:(?:"[A-Za-z_][A-Za-z0-9_]*"|[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*)*'
    r'(?:"(?P<quoted>[A-Za-z_][A-Za-z0-9_]*)"|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))',
    re.IGNORECASE,
)
_CTE_NAME_RE = re.compile(
    r'(?:\bWITH(?:\s+RECURSIVE)?|,)\s+'
    r'(?:"(?P<quoted>[^"]+)"|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))'
    r'\s*(?:\([^)]*\))?\s+AS\s*\(',
    re.IGNORECASE,
)


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


def _registered_physical_table_names() -> frozenset[str]:
    """Return queryable physical tables from the loaded semantic schema."""
    try:
        from .schema import SCHEMA
    except Exception:
        return frozenset()
    return frozenset(
        str(table.get("physical_table") or table.get("name") or "")
        .rsplit(".", 1)[-1]
        .strip('"')
        .lower()
        for table in SCHEMA.get("tables", [])
        if str(table.get("semantic_visibility") or "default").lower() != "restricted"
    )


def _sql_identifier_view(sql: str) -> str:
    """Mask literals/comments while retaining identifier positions and quotes."""
    return "".join(
        text if not protected or text.startswith('"') else " " * len(text)
        for protected, text in _split_sql_segments(sql)
    )


def _top_level_count_output_indexes(sql: str) -> list[int]:
    statements = sqlparse.parse(_sql_identifier_view(sql))
    if not statements:
        return []
    expressions: list[object] = []
    in_select = False
    for token in statements[0].tokens:
        if not in_select:
            if token.ttype is sqlparse.tokens.DML and token.normalized == "SELECT":
                in_select = True
            continue
        if token.ttype in sqlparse.tokens.Keyword and token.normalized == "FROM":
            break
        if token.is_whitespace or token.ttype is sqlparse.tokens.Punctuation:
            continue
        if token.ttype in sqlparse.tokens.Keyword and token.normalized in {"ALL", "DISTINCT"}:
            continue
        if isinstance(token, sqlparse.sql.IdentifierList):
            expressions.extend(token.get_identifiers())
        else:
            expressions.append(token)
    return [
        index
        for index, expression in enumerate(expressions)
        if _COUNT_OUTPUT_RE.search(_sql_identifier_view(str(expression)))
    ]


def _is_zero_query_result(sql: str, rows: list[tuple]) -> bool:
    if not rows:
        return True
    if len(rows) != 1:
        return False
    values = rows[0]
    count_values = [
        values[index]
        for index in _top_level_count_output_indexes(sql)
        if index < len(values)
    ]
    return bool(count_values) and all(
        isinstance(value, Number) and not isinstance(value, bool) and value == 0
        for value in count_values
    )


def _physical_table_matches(sql: str) -> list[re.Match]:
    identifier_view = _sql_identifier_view(sql)
    cte_names = {
        (match.group("quoted") or match.group("plain")).lower()
        for match in _CTE_NAME_RE.finditer(identifier_view)
    }
    return [
        match
        for match in _TABLE_REFERENCE_RE.finditer(identifier_view)
        if (match.group("quoted") or match.group("plain")).lower() not in cte_names
        or "." in match.group(0)
    ]


def _has_tbd_table_reference(sql: str) -> bool:
    return any(
        (match.group("quoted") or match.group("plain")).lower().startswith("tbd")
        for match in _physical_table_matches(sql)
    )


def _tmd_fallback_policy_matches(source_table: str, target_table: str) -> bool:
    """Keep automatic fallback inside a governed table's availability contract."""
    source_policy = accumulation_policy_for(source_table)
    if not source_policy:
        return True
    target_policy = accumulation_policy_for(target_table)
    if not target_policy:
        return False
    comparable_fields = ("cadence", "query_time_dimension", "format")
    return all(source_policy.get(field) == target_policy.get(field) for field in comparable_fields)


def _tmd_fallback_sql(sql: str, *, allow_cross_cycle_fallback: bool = True) -> str | None:
    """Swap TBD tables, optionally limiting governed tables to the same cadence."""
    identifier_view = _sql_identifier_view(sql)
    table_matches = _physical_table_matches(sql)
    referenced_tables = {
        (match.group("quoted") or match.group("plain")).lower() for match in table_matches
    }
    registered_tables = _registered_physical_table_names()
    replacements = {
        table: f"tmd{table[3:]}"
        for table in referenced_tables
        if table.startswith("tbd")
        and f"tmd{table[3:]}" in registered_tables
        and f"tmd{table[3:]}" not in referenced_tables
        and (
            allow_cross_cycle_fallback
            or _tmd_fallback_policy_matches(table, f"tmd{table[3:]}")
        )
    }
    if not replacements:
        return None

    qualifier_re = re.compile(
        r'(?<![A-Za-z0-9_])(?:"(?P<quoted>'
        + "|".join(map(re.escape, replacements))
        + r')"|(?P<plain>'
        + "|".join(map(re.escape, replacements))
        + r'))(?=\s*\.)',
        re.IGNORECASE,
    )

    def replacement_for(source: str) -> str:
        target = replacements[source.lower()]
        return target.upper() if source.isupper() else target

    spans: dict[tuple[int, int], str] = {}
    for match in table_matches:
        group = "quoted" if match.group("quoted") is not None else "plain"
        source = match.group(group)
        if source.lower() in replacements:
            spans[match.span(group)] = replacement_for(source)
    for match in qualifier_re.finditer(identifier_view):
        group = "quoted" if match.group("quoted") is not None else "plain"
        source = match.group(group)
        spans[match.span(group)] = replacement_for(source)

    fallback_sql = sql
    for (start, end), replacement in sorted(spans.items(), reverse=True):
        fallback_sql = fallback_sql[:start] + replacement + fallback_sql[end:]
    return fallback_sql if fallback_sql != sql else None


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


def _execute_postgres(
    sql: str,
    max_rows: int = _MAX_ROWS,
    statement_timeout_ms: int = _STATEMENT_TIMEOUT_MS,
) -> tuple[list[str], list[tuple]]:
    conn = None
    cur = None
    discard_connection = False
    try:
        conn = get_db_connection()
        conn.set_session(readonly=True)
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {statement_timeout_ms}")
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows) if cur.description else []
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


def _execute_athena(
    sql: str,
    max_rows: int = _MAX_ROWS,
    statement_timeout_ms: int = _STATEMENT_TIMEOUT_MS,
) -> tuple[list[str], list[tuple]]:
    conn = _get_athena_connection()
    cur = conn.cursor()
    try:
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows) if cur.description else []
        return columns, [tuple(row) for row in rows]
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Public API (backend-agnostic)
# ---------------------------------------------------------------------------
def _execute_backend(
    sql: str,
    max_rows: int = _MAX_ROWS,
    statement_timeout_ms: int = _STATEMENT_TIMEOUT_MS,
) -> tuple[list[str], list[tuple]]:
    executor = _execute_athena if DB_BACKEND == "athena" else _execute_postgres
    if max_rows == _MAX_ROWS and statement_timeout_ms == _STATEMENT_TIMEOUT_MS:
        return executor(sql)
    if statement_timeout_ms == _STATEMENT_TIMEOUT_MS:
        return executor(sql, max_rows)
    return executor(sql, max_rows, statement_timeout_ms)


def _bounded_result_sql(sql: str, max_rows: int) -> str:
    return f"SELECT * FROM (\n{sql.rstrip().removesuffix(';')}\n) AS _bounded_result\nLIMIT {max_rows}"


def execute_sql(
    sql: str,
    *,
    max_rows: int | None = None,
    statement_timeout_ms: int | None = None,
    allow_cross_cycle_fallback: bool = True,
) -> tuple[list[str], list[tuple], str | None]:
    started = time.monotonic()
    effective_max_rows = _MAX_ROWS if max_rows is None else min(max(int(max_rows), 1), MAX_QUERY_ROW_LIMIT)
    effective_timeout_ms = _STATEMENT_TIMEOUT_MS if statement_timeout_ms is None else max(int(statement_timeout_ms), 1)

    def execute_backend(query: str) -> tuple[list[str], list[tuple]]:
        # Preserve the one-argument call used by existing adapters and test doubles.
        if max_rows is None and statement_timeout_ms is None:
            return _execute_backend(query)
        bounded_query = _bounded_result_sql(query, effective_max_rows) if max_rows is not None else query
        return _execute_backend(bounded_query, effective_max_rows, effective_timeout_ms)

    stripped = sqlparse.format(sql, strip_comments=True).strip()
    execution_sql = prepare_sql_for_backend(stripped)
    safety_error = _validate_read_only_sql(execution_sql)
    if safety_error:
        _log_db_query(
            started,
            status="ERROR",
            result_count=0,
            error_message=safety_error,
            max_rows=effective_max_rows,
            statement_timeout_ms=effective_timeout_ms,
        )
        return [], [], safety_error
    try:
        columns, rows = execute_backend(execution_sql)
        if _is_zero_query_result(execution_sql, rows) and _has_tbd_table_reference(execution_sql):
            # The initial execution above plus these retries make three TBD attempts.
            for _ in range(_TBD_ZERO_RESULT_ATTEMPTS - 1):
                columns, rows = execute_backend(execution_sql)
                if not _is_zero_query_result(execution_sql, rows):
                    break
            else:
                fallback_sql = _tmd_fallback_sql(
                    execution_sql,
                    allow_cross_cycle_fallback=allow_cross_cycle_fallback,
                )
                if fallback_sql:
                    try:
                        fallback_columns, fallback_rows = execute_backend(fallback_sql)
                    except Exception:
                        # TMD is a best-effort fallback after TBD already returned
                        # a valid zero-row result; its failure must not replace it.
                        pass
                    else:
                        columns, rows = fallback_columns, fallback_rows
        _log_db_query(
            started,
            status="SUCCESS",
            result_count=len(rows),
            max_rows=effective_max_rows,
            statement_timeout_ms=effective_timeout_ms,
        )
        return columns, rows, None
    except Exception as e:
        _log_db_query(
            started,
            status="ERROR",
            result_count=0,
            error_message=str(e),
            max_rows=effective_max_rows,
            statement_timeout_ms=effective_timeout_ms,
        )
        return [], [], str(e)


def count_result_rows(sql: str) -> int | None:
    """조회 결과의 원본 행 수를 센다.

    행 상한(fetchmany)에 걸린 결과는 가져온 행 수가 곧 전체 건수가 아니다.
    "몇 건이냐"에 답하려면 잘리지 않은 개수를 DB에서 다시 물어야 한다.
    """
    statement = sqlparse.format(sql, strip_comments=True).strip().rstrip(";").strip()
    if not statement:
        return None
    _, rows, error = execute_sql(
        f"SELECT COUNT(*) FROM (\n{statement}\n) AS _row_total",
        max_rows=1,
        allow_cross_cycle_fallback=False,
    )
    if error or not rows or not rows[0]:
        return None
    try:
        return int(rows[0][0])
    except (TypeError, ValueError):
        # 전체 건수는 부가 정보다. 못 세면 "확인 불가"로 남기고 답변은 나가야 한다.
        return None


# 적재 정책표는 주기만 알 뿐 어디까지 들어왔는지는 모른다. 최신 가용일 판정과
# "데이터가 없다"의 근거는 서버 시계가 아니라 데이터에서 읽어야 한다. MIN/MAX 는
# 해당 컬럼을 훑으므로 테이블당 한 시간에 한 번만 실제로 조회한다.
_PERIOD_RANGE_TTL_SECONDS = 3600
_PERIOD_RANGE_CACHE: dict[str, tuple[float, tuple[str, str] | None]] = {}


def loaded_period_range(table: str) -> tuple[str, str] | None:
    """Return the (min, max) time-axis values a governed table actually holds."""
    column = str((accumulation_policy_for(table) or {}).get("query_time_dimension") or "")
    if not column:
        return None
    name = str(table).strip().lower()
    cached = _PERIOD_RANGE_CACHE.get(name)
    if cached and time.monotonic() - cached[0] < _PERIOD_RANGE_TTL_SECONDS:
        return cached[1]
    bounds = None
    try:
        _, rows, error = execute_sql(
            f'SELECT MIN("{column}"), MAX("{column}") FROM {DB_SCHEMA_PREFIX}{name}',
            max_rows=1,
            allow_cross_cycle_fallback=False,
        )
        if not error and rows and len(rows[0]) >= 2 and None not in rows[0][:2]:
            bounds = (str(rows[0][0]), str(rows[0][1]))
    except Exception:
        # 범위 안내는 부가 정보다. 못 읽어도 "데이터가 없다"는 답변은 나가야 한다.
        bounds = None
    _PERIOD_RANGE_CACHE[name] = (time.monotonic(), bounds)
    return bounds


def _log_db_query(
    started: float,
    *,
    status: str,
    result_count: int,
    error_message: str | None = None,
    max_rows: int = _MAX_ROWS,
    statement_timeout_ms: int = _STATEMENT_TIMEOUT_MS,
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
            "max_rows": max_rows,
            "statement_timeout_ms": statement_timeout_ms,
        },
        error_type="DatabaseQueryError" if error_message else None,
        error_message=error_message,
        log_level="ERROR" if status == "ERROR" else "INFO",
    )
