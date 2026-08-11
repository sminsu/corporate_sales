"""참고 SQL(sql_verified_queries.yaml)이 스키마와 방언에 맞는지.

이 파일의 SQL은 프롬프트의 "참고 SQL 예시"라서 모델이 문장을 그대로 베낀다.
v1의 오류 하나(special_debt_by_asset_quality)가 goldenset 실패 22건을 만들었다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from text2sql_v2.sql_dialect_guard import audit_sql
from text2sql_v2.verified_query_audit import unregistered_tables, unresolved_identifiers

V2_DIR = Path(__file__).resolve().parents[1]
SCHEMA = V2_DIR / "semantic_layer.yaml"
V2_QUERIES = V2_DIR / "sql_verified_queries.yaml"
V1_QUERIES = V2_DIR.parent / "text2sql_agent" / "tools" / "sql_verified_queries.yaml"


def _queries(path: Path) -> list[dict]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document["verified_queries"]


@pytest.fixture(scope="module")
def v2_queries() -> list[dict]:
    return _queries(V2_QUERIES)


def _inline_sql(queries: list[dict]) -> list[tuple[str, str]]:
    """sql 키가 있는 항목만. 나머지는 tools/sql_builders.py 가 만든다."""
    return [(str(q.get("name")), str(q.get("sql"))) for q in queries if str(q.get("sql") or "").strip()]


def test_query_set_is_preserved(v2_queries: list[dict]) -> None:
    v1_names = [str(q.get("name")) for q in _queries(V1_QUERIES)]
    assert [str(q.get("name")) for q in v2_queries] == v1_names


def test_no_postgres_casts_remain(v2_queries: list[dict]) -> None:
    """::FLOAT 예시를 모델이 베껴 syntax 오류 2건이 났다."""
    offenders = [name for name, sql in _inline_sql(v2_queries) if "::" in sql]
    assert offenders == []


def test_v1_actually_had_the_postgres_casts() -> None:
    """회귀 테스트가 무엇을 막고 있는지 고정한다."""
    offenders = [name for name, sql in _inline_sql(_queries(V1_QUERIES)) if "::" in sql]
    assert len(offenders) >= 7


def test_special_debt_query_uses_the_right_table(v2_queries: list[dict]) -> None:
    """COLUMN_NOT_FOUND '개인기업구분코드' 22건의 원인."""
    query = next(q for q in v2_queries if q["name"] == "special_debt_by_asset_quality")
    assert "tbmaisd06" in query["sql"]
    assert "tmdaaus01" not in query["sql"]


def test_merchant_risk_query_uses_the_right_table(v2_queries: list[dict]) -> None:
    query = next(q for q in v2_queries if q["name"] == "merchant_risk_combined_customer_month")
    assert "tbdaaus01" in query["sql"]
    assert "tbmaisd06" not in query["sql"]


def test_every_column_resolves_against_the_schema(v2_queries: list[dict]) -> None:
    failures = {}
    for name, sql in _inline_sql(v2_queries):
        if unregistered_tables(sql, SCHEMA):
            continue  # 테이블 정의가 없으면 컬럼 검증 자체가 불가능하다
        unresolved = unresolved_identifiers(sql, SCHEMA)
        if unresolved:
            failures[name] = unresolved
    assert failures == {}


def test_no_query_trips_the_dialect_guard(v2_queries: list[dict]) -> None:
    failures = {name: issues for name, sql in _inline_sql(v2_queries) if (issues := audit_sql(sql))}
    assert failures == {}


def test_unregistered_table_references_are_known(v2_queries: list[dict]) -> None:
    """미등록 테이블은 남아 있다. 원천 정의가 오면 semantic layer에 등록해야 한다."""
    referenced: set[str] = set()
    for _, sql in _inline_sql(v2_queries):
        referenced.update(unregistered_tables(sql, SCHEMA))
    assert referenced == {
        "managed_scope",   # 런타임 관리기업 목록 관계
        "tbdaaat14",
        "tbdaaaf23",
        "tbdaaha97",
        "tmdaa2e17",
    }, f"미등록 테이블 목록이 바뀌었다: {sorted(referenced)}"
