"""실행 전 컬럼 해석 — 없는 컬럼을 고치거나, 못 고치면 구체적으로 알려준다.

goldenset v2 의 SQL 실행 실패 40건 중 **27건**이 `COLUMN_NOT_FOUND` 였다.
Athena 가 거절할 때까지 아무도 못 잡는다는 게 문제였다. 정적 검증은 별칭이 붙은
`a."컬럼"` 만 보고, 실패한 SQL 은 거의 다 별칭 없는 `"컬럼"` 을 쓴다.

실패는 두 갈래다.

    스키마에 아예 없는 이름 (16건) — 모델이 컬럼명을 줄이거나 바꿔 부른다
        "교부유형"           ← 교부유형구분코드
        "금월일반이용금액"    ← 금월일반매출이용금액
        "시도"               ← 한글시도명
        "bss등급"            ← 소호로지스틱BSS등급구분코드

    있긴 한데 그 테이블에 없는 이름 (11건) — 테이블을 잘못 골랐다
        tbdaaus01."기준년월"  → 일별 테이블이라 기준년월일만 있다 (월별은 tmdaaus01)
        tbdaa1d12."모바일카드여부" → tbdaaat05 에 있다

앞의 것 중 접미사만 다른 경우는 확실하므로 그냥 고친다. 나머지는 고치지 않고
재시도 메시지에 **어디에 있는지**를 적는다. 로컬 모델에게 "컬럼이 없습니다"는
아무 정보가 아니지만 "그 컬럼은 tmdaaus01 에 있습니다"는 다음 시도를 바꾼다.

틀린 교정은 에러보다 나쁘다. 그래서 자동 교정은 두 가지만 한다.

    1. 대소문자·구분자만 다른 완전 일치
    2. derive_column_synonyms() 가 만들어 내는 접미사 변형 (구분코드/코드/명/여부)

둘 다 스코프 안에서 후보가 정확히 하나일 때만 바꾼다.
"""

from __future__ import annotations

import re

from .column_synonyms import compact, derive_column_synonyms

# FROM (SELECT ...) x 의 x, JOIN 뒤 예약어 등 테이블 별칭으로 오인되는 토큰.
_ALIAS_STOP_WORDS = frozenset(
    {
        "on", "where", "group", "order", "having", "limit", "union", "cross",
        "inner", "left", "right", "full", "outer", "join", "select", "as",
        "using", "window", "offset", "fetch", "natural", "and", "or",
    }
)

# FROM/JOIN 뒤의 테이블과 별칭. 스키마 prefix 와 따옴표를 모두 허용한다.
#
# 별칭 자리에 lookahead 를 두지 않으면 "FROM a JOIN b" 의 JOIN 이 a 의 별칭으로
# 소비돼 다음 JOIN 매치가 시작되지 않는다. 그러면 조인한 테이블이 스코프에 아예
# 안 들어가고, tbdaaat01."BSS등급구분코드" 같은 없는 컬럼이 그대로 통과한다.
_TABLE_REF_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+'
    r'(?:(?:"?[A-Za-z_][A-Za-z0-9_]*"?)\.)?'
    r'"?([A-Za-z_][A-Za-z0-9_]*)"?'
    rf'(?:\s+(?:AS\s+)?(?!(?:{"|".join(sorted(_ALIAS_STOP_WORDS))})\b)"?([A-Za-z_][A-Za-z0-9_]*)"?)?',
    re.IGNORECASE,
)
_CTE_NAME_RE = re.compile(r'(?:\bWITH\b|,)\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s+AS\s*\(', re.IGNORECASE)
_ALIAS_RE = re.compile(r'\bAS\s+(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))', re.IGNORECASE)
# 한정자 자리의 따옴표 이름은 컬럼이 아니다. 뒤쪽 lookahead 가 없으면
# `"kb"."tbdaa1d12"` 의 스키마 이름을 별칭 없는 컬럼으로 읽는다.
_QUOTED_RE = re.compile(r'(?<!\.)(?<!\w)"([^"]+)"(?!\s*\.)')
_QUALIFIED_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"([^"]+)"')

def _strip_literals(sql: str) -> str:
    """문자열 리터럴과 주석을 공백으로 지운 스캔용 SQL.

    식별자 큰따옴표는 남긴다. '%도미노피자%' 안의 글자나 주석의 한글이 컬럼으로
    잡히면 안 된다.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        if char == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(" " * (min(j, n - 1) - i + 1))
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i))
            i = j
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _cte_segments(sql: str) -> list[tuple[str, str]]:
    """(CTE 이름, 본문) 목록과 마지막 메인 쿼리 ("" 이름).

    스코프를 SQL 전체로 잡으면 "이 CTE 안에서는 없는 컬럼"을 놓친다. 실제로
    tmdaa3e16 만 있는 CTE 가 "가맹점번호" 를 쓰고, 바깥에서 tmdaaus01 을 조인해
    전체 합집합으로는 통과하던 SQL 이 Athena 에서 죽었다.
    """
    segments: list[tuple[str, str]] = []
    end = 0
    for match in _CTE_NAME_RE.finditer(sql):
        open_paren = sql.index("(", match.end() - 1)
        depth, i, n = 0, open_paren, len(sql)
        while i < n:
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            return [("", sql)]
        segments.append((match.group(1).lower(), sql[open_paren + 1 : i]))
        end = i + 1
    segments.append(("", sql[end:] if segments else sql))
    return segments


class _Scope:
    """한 구간(CTE 본문 또는 메인 쿼리)의 이름 해석 범위."""

    def __init__(self) -> None:
        self.alias_tables: dict[str, set[str]] = {}
        self.available: set[str] = set()
        self.defined: set[str] = set()
        self.sources: set[str] = set()   # 이 구간이 읽는 테이블·CTE 이름
        self.opaque = False              # 정체를 모르는 원천이 섞여 있다


def _segment_scope(
    body: str,
    table_columns: dict[str, set[str]],
    cte_names: set[str],
    provides: dict[str, set[str]],
) -> _Scope:
    scope = _Scope()
    for match in _TABLE_REF_RE.finditer(body):
        table = match.group(1).lower()
        alias = (match.group(2) or table).lower()
        if alias in _ALIAS_STOP_WORDS:
            alias = table
        if table in cte_names:
            scope.sources.add(table)
            scope.available |= provides.get(table, set())
            continue
        if table not in table_columns:
            scope.opaque = True
            continue
        scope.sources.add(table)
        scope.alias_tables.setdefault(alias, set()).add(table)
        scope.alias_tables.setdefault(table, set()).add(table)
        scope.available |= table_columns[table]

    scope.defined = {quoted or bare for quoted, bare in _ALIAS_RE.findall(body)}
    return scope


def _split_inline_subqueries(body: str) -> tuple[str, list[str]]:
    """``FROM ( SELECT ... )`` 안쪽을 떼어내 별도 구간으로 만든다.

    떼어내지 않고 통째로 opaque 처리하면 안쪽에서 쓰는 없는 컬럼을 놓친다.
    실제로 ``FROM (SELECT ... ORDER BY "기준년월일" ... FROM tbdaaat05) t`` 가
    그렇게 빠져나갔다. 바깥에는 공백을 남겨 위치를 보존한다.
    """
    inner: list[str] = []
    out = list(body)
    for match in re.finditer(r"\bFROM\s*\(", body, re.IGNORECASE):
        start = body.index("(", match.end() - 1)
        depth, i, n = 0, start, len(body)
        while i < n:
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0 or not re.search(r"\bSELECT\b", body[start:i], re.IGNORECASE):
            continue
        inner.append(body[start + 1 : i])
        out[start + 1 : i] = " " * (i - start - 1)
    return "".join(out), inner


def _date_grain(name: str) -> str:
    """컬럼명이 드러내는 날짜 단위. 다르면 자동 교정하면 안 된다."""
    text = compact(name)
    for suffix, grain in (("년월일", "day"), ("년월", "month"), ("기준년", "year")):
        if text.endswith(suffix):
            return grain
    return ""


def _resolve(name: str, candidates: set[str]) -> str:
    """스코프 안에서 확실하게 대응되는 컬럼 하나. 없거나 여럿이면 빈 문자열."""
    target = compact(name).lower()
    exact = {column for column in candidates if compact(column).lower() == target}
    if len(exact) == 1:
        return next(iter(exact))
    if exact:
        return ""

    by_synonym = {
        column
        for column in candidates
        if any(
            compact(variant).lower() == target
            for variant in derive_column_synonyms(column)
        )
    }
    if len(by_synonym) == 1:
        return next(iter(by_synonym))
    if by_synonym:
        return ""

    # 후보가 딱 하나뿐인 근사 이름은 오타·축약으로 본다.
    #   "CD기능구분코드"    → CD기기능구분코드
    #   "금월일반이용금액"   → 금월일반매출이용금액
    # 단 날짜 단위가 달라지는 교정은 절대 하지 않는다. "기준년월"(YYYYMM)을
    # "기준년월일"(YYYYMMDD)로 바꾸면 에러 대신 조용히 틀린 값이 나온다.
    near = _nearby(name, candidates, limit=2)
    if len(near) == 1 and _date_grain(name) == _date_grain(near[0]):
        return near[0]
    return ""


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(char in it for char in short)


def _nearby(name: str, candidates: set[str], limit: int = 4) -> list[str]:
    """재시도 힌트용 후보.

    부분문자열만 보면 모델이 가운데 토막을 빼먹은 경우를 놓친다.
    "금월일반이용금액" 은 "금월일반매출이용금액" 의 부분문자열이 아니지만
    부분수열이다. 길이 비율로 엉뚱한 짝을 걸러낸다.
    """
    target = compact(name).lower()
    if not target:
        return []
    hits = []
    for column in candidates:
        other = compact(column).lower()
        if not other:
            continue
        short, long = (target, other) if len(target) <= len(other) else (other, target)
        if len(short) / len(long) < 0.6:
            continue
        if _is_subsequence(short, long):
            hits.append(column)
    return sorted(hits, key=lambda column: (abs(len(column) - len(name)), column))[:limit]


def repair_columns(
    sql: str,
    table_columns: dict[str, set[str]],
    *,
    column_owners: dict[str, list[str]] | None = None,
) -> tuple[str, list[str]]:
    """없는 컬럼을 고칠 수 있으면 고치고, 못 고친 것은 issue 로 돌려준다.

    ``table_columns``  물리 테이블명 → 컬럼명 집합
    ``column_owners``  컬럼명 → 그 컬럼을 가진 테이블 목록 (재시도 힌트용)
    """
    scan = _strip_literals(sql)
    segments = _cte_segments(scan)
    cte_names = {name for name, _ in segments if name}
    owners = column_owners or {}
    rewrites: dict[str, str] = {}
    issues: list[str] = []
    seen: set[tuple[str, str]] = set()
    provides: dict[str, set[str]] = {}

    def inspect(name: str, candidates: set[str], where: str, defined: set[str]) -> None:
        if name in candidates or name in defined:
            return
        key = (where, name)
        if key in seen:
            return
        seen.add(key)
        resolved = _resolve(name, candidates)
        if resolved:
            rewrites[name] = resolved
            return

        detail = f'{where}에 "{name}" 컬럼이 없습니다.'
        hint = ", ".join(f'"{column}"' for column in _nearby(name, candidates))
        if hint:
            detail += f" 이 테이블의 유사 컬럼: {hint}."
        elsewhere = ", ".join(f'{table}."{name}"' for table in owners.get(name, [])[:3])
        if elsewhere:
            detail += f" 그 이름은 {elsewhere} 에 있으니 해당 테이블을 쓰거나 조인하세요."
        elif not hint:
            # 스코프 안에도 없고 같은 이름이 스키마에도 없다. 모델이 이름을 지어낸
            # 경우("시도" ← 한글시도명, "교부유형" ← 교부유형구분코드)라 스키마
            # 전체에서 비슷한 이름을 찾아 어느 테이블에 있는지까지 알려준다.
            across = [
                f'{owners[column][0]}."{column}"'
                for column in _nearby(name, set(owners))
                if owners.get(column)
            ]
            if across:
                detail += f" 비슷한 컬럼: {', '.join(across[:3])}."
        if detail.endswith("없습니다."):
            detail += " 테이블 상세에 있는 컬럼명을 그대로 쓰세요."
        issues.append(detail)

    for cte_name, whole in segments:
        outer, inline = _split_inline_subqueries(whole)
        bodies = [*inline, outer]
        exported: set[str] = set()
        for index, body in enumerate(bodies):
            scope = _segment_scope(body, table_columns, cte_names, provides)
            if index < len(bodies) - 1:
                # 인라인 서브쿼리가 바깥으로 내보내는 이름.
                exported |= scope.available | scope.defined
            else:
                scope.available |= exported
                scope.defined |= exported
                if cte_name:
                    provides[cte_name] = scope.available | scope.defined
            if not scope.available:
                continue
            where = "/".join(sorted(scope.sources)) or (cte_name or "선택 테이블")

            # 별칭이 붙은 참조는 원천 컬럼이 확실하다. 같은 이름의 출력 alias가
            # 있어도(m."부점코드" AS "부점코드") 원천에 없으면 없는 것이다.
            for alias, name in _QUALIFIED_RE.findall(body):
                tables = scope.alias_tables.get(alias.lower())
                if not tables:
                    continue
                inspect(
                    name,
                    set().union(*(table_columns[table] for table in tables)),
                    "/".join(sorted(tables)),
                    set(),
                )

            if scope.opaque:
                continue
            for name in _QUOTED_RE.findall(body):
                # FROM "tbdaa1d12" 처럼 테이블·CTE 이름을 따옴표로 감싼 참조는
                # 별칭 없는 컬럼과 형태가 같다. 그대로 두면 테이블명 자체가 없는
                # 컬럼으로 보고돼 재시도를 다 태우고도 못 고친다. 어차피 컬럼명과
                # 테이블명이 겹치는 경우는 스키마에 없다.
                if name.lower() in scope.sources:
                    continue
                inspect(name, scope.available, where, scope.defined)

    if not rewrites:
        return sql, issues

    repaired = sql
    for wrong, right in rewrites.items():
        repaired = repaired.replace(f'"{wrong}"', f'"{right}"')
    return repaired, issues
