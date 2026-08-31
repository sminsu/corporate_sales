"""참고 SQL(sql_verified_queries.yaml)이 스키마와 방언에 맞는지.

이 파일의 SQL은 프롬프트의 "참고 SQL 예시"라서 모델이 문장을 그대로 베낀다.
v1의 오류 하나(special_debt_by_asset_quality)가 goldenset 실패 22건을 만들었다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from text2sql_agent.v2.sql_dialect_guard import audit_sql
from text2sql_agent.v2.verified_query_audit import unregistered_tables, unresolved_identifiers
from scripts.v2_build.build_verified_queries_v2 import (
    HISTORICAL_CORPORATE_MONTH_QUERIES,
    MERCHANT_MASTER_QUERIES,
    apply_fixes,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]          # v2 적용본 루트
V1_ROOT = PROJECT_ROOT.parent / "corporate_sales_fable"     # 변환 원본(v1 저장소)
SCHEMA = PROJECT_ROOT / "semantic_layer.yaml"
V2_QUERIES = PROJECT_ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml"
V1_QUERIES = V1_ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml"

requires_v1 = pytest.mark.skipif(
    not V1_QUERIES.exists(), reason=f"v1 원본 저장소가 없다: {V1_ROOT}"
)


def _queries(path: Path) -> list[dict]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document["verified_queries"]


@pytest.fixture(scope="module")
def v2_queries() -> list[dict]:
    return _queries(V2_QUERIES)


def _inline_sql(queries: list[dict]) -> list[tuple[str, str]]:
    """sql 키가 있는 항목만. 나머지는 tools/sql_builders.py 가 만든다."""
    return [(str(q.get("name")), str(q.get("sql"))) for q in queries if str(q.get("sql") or "").strip()]


# v2 가 새로 세운 참고 SQL. 이 목록에 없는 추가·삭제는 드리프트다.
V2_ADDED_QUERIES = ("merchant_sales_by_region_comparison",)


@requires_v1
def test_query_set_is_preserved(v2_queries: list[dict]) -> None:
    v1_names = [str(q.get("name")) for q in _queries(V1_QUERIES)]
    v2_names = [str(q.get("name")) for q in v2_queries]

    assert [name for name in v2_names if name not in V2_ADDED_QUERIES] == v1_names
    assert [name for name in v2_names if name in V2_ADDED_QUERIES] == list(V2_ADDED_QUERIES)


def test_no_postgres_casts_remain(v2_queries: list[dict]) -> None:
    """::FLOAT 예시를 모델이 베껴 syntax 오류 2건이 났다."""
    offenders = [name for name, sql in _inline_sql(v2_queries) if "::" in sql]
    assert offenders == []


@requires_v1
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
    assert "tmdaaus01" in query["sql"]
    assert "tbdaaus01" not in query["sql"]
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


def test_merchant_master_queries_do_not_bound_their_load_window(v2_queries: list[dict]) -> None:
    """마스터는 가맹점번호 1건이 grain 이다. 묻지 않은 기간을 답에 섞지 않는다.

    v2 초기에는 이 넷에 최근 10일 실적기준년월일 창을 얹었다. 질문이 날짜를 말하면
    실행 직전에 _apply_tbdaadt01_historical_source 가 그 날짜로 조건을 넣는다.
    """
    by_name = {query["name"]: query for query in v2_queries}

    for name in MERCHANT_MASTER_QUERIES:
        sql = by_name[name]["sql"]
        assert "card_system.tbdaadt01" in sql
        assert "실적기준년월일" not in sql


def test_historical_corporate_queries_use_the_monthly_load_boundary(v2_queries: list[dict]) -> None:
    by_name = {query["name"]: query for query in v2_queries}

    for name in HISTORICAL_CORPORATE_MONTH_QUERIES:
        sql = by_name[name]["sql"]
        assert "card_system.tmdaa1d12" in sql
        assert "card_system.tbdaa1d12" not in sql
        assert 'a."기준년월"' in sql


def test_current_and_historical_corporate_sources_are_split(v2_queries: list[dict]) -> None:
    by_name = {query["name"]: query for query in v2_queries}
    mixed_sql = by_name["current_corporate_half_year_usage_decline"]["sql"]
    current_only_sql = by_name["current_corporate_member_half_year_new_check_card_issuance"]["sql"]

    assert mixed_sql.count("card_system.tbdaa1d12") == 1
    assert mixed_sql.count("card_system.tmdaa1d12") == 1
    assert 'WHERE a."기준년월일" = l."현재기준일"' in mixed_sql
    assert 'FROM card_system.tmdaa1d12 a\n  WHERE a."기준년월"' in mixed_sql

    assert "card_system.tbdaa1d12" in current_only_sql
    assert "card_system.tmdaa1d12" not in current_only_sql
    assert "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul')" in current_only_sql


@requires_v1
def test_cadence_transforms_are_idempotent() -> None:
    document = yaml.safe_load(V1_QUERIES.read_text(encoding="utf-8"))

    apply_fixes(document)
    once = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    apply_fixes(document)

    assert yaml.safe_dump(document, allow_unicode=True, sort_keys=False) == once
