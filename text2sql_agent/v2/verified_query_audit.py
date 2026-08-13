"""참고 SQL이 semantic layer 스키마와 맞는지 검사한다.

sql_verified_queries.yaml 의 SQL은 프롬프트의 "참고 SQL 예시"로 들어가서 모델이
문장 구조를 그대로 베낀다. 그래서 이 파일의 테이블·컬럼 오류는 모델 오류가 된다.
실제로 special_debt_by_asset_quality 한 건이 goldenset v2 실패 22건을 만들었다.

여기서 하는 검사는 하나다. SQL이 FROM/JOIN 한 테이블들의 컬럼 집합으로 SQL에 나온
한글 식별자가 전부 설명되는가. 안 되면 테이블이 틀렸거나 컬럼명이 틀렸다.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import yaml

from .sql_dialect_guard import _strip_strings_and_comments

_TABLE_RE = re.compile(r"(?<![0-9A-Za-z_])(?:FROM|JOIN)\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?([A-Za-z][A-Za-z0-9_]{3,})")
_CTE_RE = re.compile(r"(?<![0-9A-Za-z_])([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", re.IGNORECASE)
# 식별자는 ASCII로 시작할 수 있다: "MCC업종코드", "금월CA이용금액", alias "m_5_년월".
_IDENTIFIER_BODY = r"[A-Za-z0-9_]*[가-힣][가-힣A-Za-z0-9_]*"
_ALIAS_RE = re.compile(rf"(?<![0-9A-Za-z_])AS\s+\"?({_IDENTIFIER_BODY})\"?", re.IGNORECASE)
_KOREAN_RE = re.compile(rf"(?<![0-9A-Za-z_]){_IDENTIFIER_BODY}")
# {기간_시작} 같은 파라미터 자리는 컬럼이 아니다.
_PARAM_RE = re.compile(r"\{[^{}]*\}")


@functools.lru_cache(maxsize=8)
def _table_columns(schema_path: str) -> dict[str, frozenset[str]]:
    schema = yaml.safe_load(Path(schema_path).read_text(encoding="utf-8"))
    columns: dict[str, frozenset[str]] = {}
    for table in schema.get("tables", []):
        names = {
            str(column.get("name"))
            for section in ("dimensions", "measures", "time_dimensions")
            for column in table.get(section) or []
            if column.get("name")
        }
        frozen = frozenset(names)
        for key in (table.get("name"), table.get("physical_table")):
            if key:
                columns[str(key).rsplit(".", 1)[-1].lower()] = frozen
    return columns


def unresolved_identifiers(sql: str, schema_path: Path | str) -> list[str]:
    """SQL의 한글 식별자 중 참조 테이블 컬럼으로 설명되지 않는 것들."""
    # CASE WHEN ... THEN '급증' 같은 문자열 리터럴은 컬럼이 아니므로 먼저 지운다.
    text = _strip_strings_and_comments(str(sql or ""))
    text = _PARAM_RE.sub(" ", text)
    tables = [name.lower() for name in _TABLE_RE.findall(text)]
    known = _table_columns(str(schema_path))
    referenced = [name for name in tables if name in known]
    if not referenced:
        return []

    # Athena는 식별자 대소문자를 구분하지 않는다(금월ca이용금액 = 금월CA이용금액).
    universe: set[str] = set()
    for name in referenced:
        universe |= {column.lower() for column in known[name]}

    # CTE 이름과 SELECT alias 는 스키마 컬럼이 아니어도 정상이다.
    exempt = {name.lower() for name in _CTE_RE.findall(text)}
    exempt |= {name.lower() for name in _ALIAS_RE.findall(text)}

    unresolved = {
        token
        for token in _KOREAN_RE.findall(text)
        if token.lower() not in universe and token.lower() not in exempt and len(token) >= 2
    }
    return sorted(unresolved)


def unregistered_tables(sql: str, schema_path: Path | str) -> list[str]:
    """semantic layer 에 등록되지 않은 테이블 참조.

    참고 SQL이 미등록 테이블을 쓰면 모델에게 존재하지 않는 테이블을 가르치게 된다.
    컬럼 검증도 불가능하므로 따로 보고한다.
    """
    text = _PARAM_RE.sub(" ", _strip_strings_and_comments(str(sql or "")))
    known = _table_columns(str(schema_path))
    ctes = {name.lower() for name in _CTE_RE.findall(text)}
    return sorted(
        {
            name.lower()
            for name in _TABLE_RE.findall(text)
            if name.lower() not in known and name.lower() not in ctes
        }
    )
