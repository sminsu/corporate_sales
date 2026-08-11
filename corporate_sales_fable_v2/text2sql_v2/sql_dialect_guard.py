"""Athena(Trino) 방언 가드.

goldenset v2 의 syntax 계열 실패 13건 중 12건을 유형별로 잡는다.

    4건  QUALIFY 절                      Athena에 없는 문법 (Snowflake/Teradata)
    3건  WHERE 절 안의 ROW_NUMBER()      EXPRESSION_NOT_SCALAR
    2건  expr::FLOAT                     PostgreSQL 캐스트 문법
    2건  출력 잘림                        mismatched input '<EOF>' / 괄호 불균형
    1건  WHERE 없이 붙은 AND 조건         mismatched input 'AND'

`::FLOAT` 는 프롬프트에 들어가는 참고 SQL(sql_verified_queries.yaml)에 8곳 있었고,
실패한 agent SQL은 그 예시를 그대로 베꼈다. 그래서 rewrite_postgres_casts() 는
런타임 교정과 참고 SQL 정리에 같이 쓴다.

QUALIFY와 윈도함수 WHERE는 자동 변환이 위험하므로 교정하지 않고 audit_sql() 이
재시도 프롬프트에 넣을 지시문을 돌려준다.

남은 1건(EXPRESSION_NOT_AGGREGATE: GROUP BY 쿼리에서 CROSS JOIN 스칼라를 집계 없이
SELECT)은 정적 감사에서 뺐다. CTE마다 SELECT 블록을 나눠 봐야 정확히 판정할 수 있고,
블록 구분 없이 검사하면 정상 참고 쿼리 4개를 오탐해 재시도 루프에 빠졌다.
이 유형은 sql_contract.ATHENA_RULES_V2 의 프롬프트 규칙으로만 예방한다.
"""

from __future__ import annotations

import re

_CAST_RE = re.compile(r"::\s*([A-Za-z][A-Za-z0-9_]*)(?:\s*(\([^()]*\)))?")

# Trino/Athena engine v3 에 없는 타입 이름은 대응 타입으로 바꾼다.
# ::FLOAT 는 실수 나눗셈을 노린 표현이므로 DOUBLE 이 의도에 맞는다.
_TYPE_ALIASES = {
    "FLOAT": "DOUBLE",
    "FLOAT8": "DOUBLE",
    "INT": "INTEGER",
    "INT4": "INTEGER",
    "INT8": "BIGINT",
    "NUMERIC": "DECIMAL",
    "TEXT": "VARCHAR",
    "STRING": "VARCHAR",
}
_QUALIFY_RE = re.compile(r"(?<![0-9A-Za-z_])QUALIFY(?![0-9A-Za-z_])", re.IGNORECASE)
_WINDOW_FN_RE = re.compile(
    r"(?<![0-9A-Za-z_])(ROW_NUMBER|RANK|DENSE_RANK|NTILE|LAG|LEAD|FIRST_VALUE|LAST_VALUE)\s*\(",
    re.IGNORECASE,
)
_PROSE_MARKERS = (
    "알려주시면",
    "알려주세요",
    "알려 주시",
    "존재하지 않습니다",
    "정의되어 있지 않습니다",
    "포함되어 있지 않습니다",
    "명시되어 있지 않습니다",
    "확인해 주시",
    "죄송",
)


# ---------------------------------------------------------------------------
# 캐스트 교정
# ---------------------------------------------------------------------------
def _operand_start(sql: str, end: int) -> int:
    """sql[:end] 의 끝에 있는 피연산자가 시작하는 위치.

    `SUM(기대대손충당금)::FLOAT` 는 `SUM(...)` 전체가, `(SUM(a) - SUM(b))::FLOAT` 는
    괄호 묶음 전체가 피연산자다. 뒤에서 앞으로 괄호 균형을 맞춰 찾는다.
    """
    index = end
    while index > 0 and sql[index - 1].isspace():
        index -= 1

    if index > 0 and sql[index - 1] == ")":
        depth = 0
        while index > 0:
            index -= 1
            char = sql[index]
            if char == ")":
                depth += 1
            elif char == "(":
                depth -= 1
                if depth == 0:
                    break
        # 괄호 앞에 함수명이 붙어 있으면 함수 호출 전체가 피연산자다.
        start = index
        while start > 0 and (sql[start - 1].isalnum() or sql[start - 1] in "_."):
            start -= 1
        return start

    # 따옴표로 감싼 식별자: "금액"::FLOAT
    if index > 0 and sql[index - 1] == '"':
        start = sql.rfind('"', 0, index - 1)
        return start if start >= 0 else index

    start = index
    while start > 0 and (sql[start - 1].isalnum() or sql[start - 1] in "_.-￿" or ord(sql[start - 1]) > 127):
        start -= 1
    return start


def rewrite_postgres_casts(sql: str) -> str:
    """expr::TYPE → CAST(expr AS TYPE). Athena는 :: 캐스트를 파싱하지 못한다.

    문자열 리터럴 안의 '::' 는 데이터이므로 건드리지 않는다. 리터럴·주석을 공백으로
    바꾼 사본에서 위치를 찾고, 원본에는 뒤에서 앞으로 치환해 오프셋이 어긋나지 않게 한다.
    """
    text = str(sql or "")
    scrubbed = _strip_strings_and_comments(text)

    edits: list[tuple[int, int, str]] = []
    for match in _CAST_RE.finditer(scrubbed):
        type_name = match.group(1).upper()
        type_name = _TYPE_ALIASES.get(type_name, type_name)
        if match.group(2):
            type_name += match.group(2)
        start = _operand_start(scrubbed, match.start())
        operand = text[start : match.start()].strip()
        if not operand:
            continue
        edits.append((start, match.end(), f"CAST({operand} AS {type_name})"))

    for start, end, replacement in reversed(edits):
        text = text[:start] + replacement + text[end:]
    return text


def normalize_sql(sql: str) -> str:
    """실행 전에 안전하게 자동 교정할 수 있는 방언 차이만 고친다."""
    return rewrite_postgres_casts(sql)


# ---------------------------------------------------------------------------
# 프로즈(자연어 되묻기) 가드
# ---------------------------------------------------------------------------
def looks_like_sql(text: str) -> bool:
    return bool(re.match(r"^\s*(?:--[^\n]*\n\s*)*(SELECT|WITH)\b", str(text or ""), re.IGNORECASE))


def looks_like_prose(text: str) -> bool:
    """모델이 SQL 대신 되물었는지 판정한다."""
    body = str(text or "").strip()
    if not body:
        return True
    if looks_like_sql(body):
        return False
    return True


def prose_reason(text: str) -> str:
    """되묻기 응답에서 재시도 프롬프트에 쓸 사유를 만든다."""
    body = str(text or "").strip()
    if not body:
        return "모델이 빈 응답을 반환했다."
    hits = [marker for marker in _PROSE_MARKERS if marker in body]
    if hits:
        return (
            "모델이 SQL 대신 자연어로 되물었다(감지된 표현: "
            + ", ".join(hits[:3])
            + "). 컬럼을 되묻지 말고 테이블 상세의 컬럼명·synonyms·value_semantics로 매핑해 SQL을 완성해야 한다."
        )
    return "응답이 SELECT/WITH로 시작하지 않는다. 설명 없이 단일 SQL만 반환해야 한다."


# ---------------------------------------------------------------------------
# 정적 감사
# ---------------------------------------------------------------------------
def _strip_strings_and_comments(sql: str) -> str:
    """리터럴·주석 안의 키워드가 오탐을 만들지 않게 공백으로 지운다."""
    out: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "'":
            end = index + 1
            while end < length:
                if sql[end] == "'":
                    if end + 1 < length and sql[end + 1] == "'":
                        end += 2
                        continue
                    break
                end += 1
            out.append(" " * (min(end, length - 1) - index + 1))
            index = end + 1
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end < 0 else end
            out.append(" " * (end - index))
            index = end
        elif sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            end = length if end < 0 else end + 2
            out.append(" " * (end - index))
            index = end
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _where_clause_spans(sql: str) -> list[tuple[int, int]]:
    """WHERE 절의 범위. 다음 절 키워드나 괄호 종료까지로 본다."""
    spans: list[tuple[int, int]] = []
    boundary = re.compile(
        r"(?<![0-9A-Za-z_])(GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|UNION|INTERSECT|EXCEPT|WINDOW|QUALIFY)"
        r"(?![0-9A-Za-z_])",
        re.IGNORECASE,
    )
    for match in re.finditer(r"(?<![0-9A-Za-z_])WHERE(?![0-9A-Za-z_])", sql, re.IGNORECASE):
        start = match.end()
        depth = 0
        end = len(sql)
        for index in range(start, len(sql)):
            char = sql[index]
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    end = index
                    break
                depth -= 1
        stop = boundary.search(sql, start, end)
        if stop:
            end = stop.start()
        spans.append((start, end))
    return spans


# FROM joined\nAND "가맹점명" LIKE ... 처럼 WHERE 없이 조건이 이어붙은 모양.
_ORPHAN_CONJUNCTION_RE = re.compile(
    r"(?<![0-9A-Za-z_])FROM\s+[\"'`\w.]+(?:\s+(?:AS\s+)?[A-Za-z_]\w*)?\s+(AND|OR)(?![0-9A-Za-z_])",
    re.IGNORECASE,
)


def audit_sql(sql: str) -> list[str]:
    """실행하면 Athena가 거절할 구문을 찾아 재시도 지시문으로 돌려준다."""
    text = str(sql or "")
    if not text.strip():
        return ["빈 SQL이다."]

    issues: list[str] = []
    scrubbed = _strip_strings_and_comments(text)

    if _CAST_RE.search(scrubbed):
        issues.append(
            "PostgreSQL 캐스트(expr::type)는 Athena 문법 오류다. CAST(expr AS DOUBLE) 형태로 바꿔라."
        )

    if _QUALIFY_RE.search(scrubbed):
        issues.append(
            "QUALIFY 절은 Athena에 없다. 서브쿼리에서 ROW_NUMBER() OVER (...) AS rn 을 만들고 "
            "바깥 쿼리에서 WHERE rn = 1 로 걸러라."
        )

    for start, end in _where_clause_spans(scrubbed):
        window = _WINDOW_FN_RE.search(scrubbed, start, end)
        if window:
            issues.append(
                f"WHERE 절에 윈도함수 {window.group(1).upper()}() 를 직접 쓸 수 없다(EXPRESSION_NOT_SCALAR). "
                "서브쿼리나 CTE에서 순번 컬럼을 만든 뒤 바깥에서 걸러라."
            )
            break

    orphan = _ORPHAN_CONJUNCTION_RE.search(scrubbed)
    if orphan:
        issues.append(
            f"FROM 절 뒤에 WHERE 없이 {orphan.group(1).upper()} 조건이 붙었다. "
            "조건은 WHERE로 시작하고, CTE에서 이미 걸렀다면 그 조건을 CTE 안으로 옮겨라."
        )

    if scrubbed.count("(") != scrubbed.count(")"):
        issues.append(
            "괄호가 맞지 않는다. SQL이 중간에 끊겼는지 확인하고 CTE·GROUP BY·ORDER BY까지 끝까지 완성해라."
        )

    if re.search(r"(?<![0-9A-Za-z_])(SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|AND|OR|,)\s*$", scrubbed.strip(), re.IGNORECASE):
        issues.append("SQL이 절 중간에서 끝났다. 끊긴 부분부터 완성해서 전체 SQL을 다시 반환해라.")

    return issues
